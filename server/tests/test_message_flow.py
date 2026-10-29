import asyncio
import json
import os
import sys
import tempfile
import unittest
import wave
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

TEMP = tempfile.TemporaryDirectory()
os.environ['VOICE_DATA_DIR'] = TEMP.name
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import server
from aiohttp import web
from aiohttp.test_utils import TestClient, TestServer


def fake_tts(text, path):
    with wave.open(str(path), 'wb') as wav:
        wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
        wav.writeframes(b'\x01\x02' * 800)


def event(message_id, text='hello', sender_type='user'):
    return SimpleNamespace(event=SimpleNamespace(
        message=SimpleNamespace(message_id=message_id, message_type='text', content=json.dumps({'text': text})),
        sender=SimpleNamespace(sender_type=sender_type, sender_id=SimpleNamespace(open_id='ou_test'))))


class MessageFlowTest(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        server.server_loop = asyncio.get_running_loop()
        with server.message_lock:
            server.message_db.execute('DELETE FROM messages')
            server.message_db.commit()
            server.unread_messages.clear()
        self.tts = patch.object(server, 'sync_generate_tts', fake_tts)
        self.reaction = patch.object(server, 'clear_and_set_message_reaction')
        self.tts.start()
        self.reaction.start()
        app = web.Application()
        app.router.add_get('/ws/device', server.ws_handler)
        app.router.add_get('/api/audio/{message_id}.pcm', server.handle_get_pcm)
        app.router.add_post('/api/upload_audio', server.handle_upload_audio)
        self.client = TestClient(TestServer(app))
        await self.client.start_server()
        self.ws = await self.client.ws_connect('/ws/device')
        self.assertEqual((await self.ws.receive_json())['unread_count'], 0)

    async def asyncTearDown(self):
        await self.ws.close()
        await self.client.close()
        self.tts.stop()
        self.reaction.stop()

    async def wait_count(self, count):
        for _ in range(100):
            if server.queue_snapshot()['unread_count'] == count:
                return
            await asyncio.sleep(0.01)
        self.fail(f'Queue did not reach {count}: {server.queue_snapshot()}')

    async def test_dedup_fifo_and_idempotent_ack(self):
        server.on_feishu_message(event('om_one'))
        server.on_feishu_message(event('om_one'))
        server.on_feishu_message(event('om_bot', sender_type='app'))
        server.on_feishu_message(event('om_two'))
        await self.wait_count(2)
        self.assertEqual(server.queue_snapshot(), {'unread_count': 2, 'head_id': 'om_one'})
        response = await self.client.get('/api/audio/om_one.pcm')
        self.assertEqual(await response.read(), b'\x01\x02' * 800)
        await self.ws.send_json({'action': 'played', 'message_id': 'om_one'})
        await self.wait_count(1)
        await self.ws.send_json({'action': 'played', 'message_id': 'om_one'})
        await asyncio.sleep(0.05)
        self.assertEqual(server.queue_snapshot(), {'unread_count': 1, 'head_id': 'om_two'})
        await self.ws.close()
        self.ws = await self.client.ws_connect('/ws/device')
        self.assertEqual(await self.ws.receive_json(), {'type': 'queue_state', 'unread_count': 1, 'head_id': 'om_two'})
        await self.ws.send_json({'action': 'played', 'message_id': 'om_two'})
        await self.wait_count(0)
        server.on_feishu_message(event('om_one'))
        await asyncio.sleep(0.05)
        self.assertEqual(server.queue_snapshot()['unread_count'], 0)
        rows = dict(server.message_db.execute('SELECT msg_id, status FROM messages'))
        self.assertEqual(rows, {'om_one': 'played', 'om_two': 'played'})

    async def test_interrupted_processing_is_recovered(self):
        with server.message_lock:
            server.message_db.execute(
                "INSERT INTO messages (msg_id, status, message_type, content, sender_id) VALUES (?, 'processing', ?, ?, ?)",
                ('om_recover', 'text', json.dumps({'text': 'recover'}), 'ou_test'))
            server.message_db.commit()
        server.recover_processing_messages()
        await self.wait_count(1)
        self.assertEqual(server.queue_snapshot()['head_id'], 'om_recover')
        self.assertEqual(server.message_db.execute("SELECT status FROM messages WHERE msg_id='om_recover'").fetchone()[0], 'ready')

    async def test_reply_retry_is_idempotent_and_uncertain_is_not_resent(self):
        server.current_open_id = 'ou_test'
        with patch.object(server, 'upload_and_send_voice_to_feishu', return_value=True) as send:
            for _ in range(2):
                response = await self.client.post('/api/upload_audio', data=b'voice', headers={'X-Recording-ID': 'abc123'})
                self.assertEqual((await response.json())['status'], 'ok')
            self.assertEqual(send.call_count, 1)
        with server.message_lock:
            server.message_db.execute("INSERT INTO reply_sends (recording_id,status) VALUES ('legacyunknown','unknown')")
            server.message_db.commit()
        with patch.object(server, 'upload_and_send_voice_to_feishu') as send:
            response = await self.client.post('/api/upload_audio', data=b'voice', headers={'X-Recording-ID': 'legacyunknown'})
            self.assertEqual((await response.json())['status'], 'unknown')
            self.assertEqual(send.call_count, 0)

    async def test_uncertain_send_recovers_with_same_uuid_inside_window(self):
        server.current_open_id = 'ou_new'
        with server.message_lock:
            server.message_db.execute("INSERT INTO reply_sends (recording_id,status,recipient,file_key,first_send_at) VALUES (?,?,?,?,?)", ('recover123', 'unknown', 'ou_original', 'file1', int(server.time.time())))
            server.message_db.commit()
        with patch.object(server, 'upload_and_send_voice_to_feishu', return_value=True) as send:
            response = await self.client.post('/api/upload_audio', data=b'voice', headers={'X-Recording-ID': 'recover123'})
            self.assertEqual((await response.json())['status'], 'ok')
            self.assertEqual(send.call_args.args[1:], ('ou_original', 'recover123', 'file1'))

    async def test_uncertain_old_send_is_not_replayed(self):
        server.current_open_id = 'ou_test'
        with server.message_lock:
            server.message_db.execute("INSERT INTO reply_sends (recording_id,status,recipient,file_key,first_send_at) VALUES (?,?,?,?,?)", ('expired123', 'unknown', 'ou_test', 'file1', int(server.time.time()) - 4000))
            server.message_db.commit()
        with patch.object(server, 'upload_and_send_voice_to_feishu') as send:
            response = await self.client.post('/api/upload_audio', data=b'voice', headers={'X-Recording-ID': 'expired123'})
            self.assertEqual((await response.json())['status'], 'unknown')
            send.assert_not_called()

    async def test_feishu_request_uses_stable_uuid_and_records_attempt(self):
        with server.message_lock:
            server.message_db.execute("INSERT INTO reply_sends (recording_id,status,recipient,file_key) VALUES (?,?,?,?)", ('stable123', 'sending', 'ou_test', 'file1'))
            server.message_db.commit()
        response = SimpleNamespace(json=lambda: {'code': 0})
        with patch.object(server, 'get_tenant_access_token', return_value='test-token'), patch.object(server.requests, 'post', return_value=response) as post:
            self.assertTrue(server.send_voice_file_to_feishu('ou_test', 'file1', 'stable123'))
            self.assertEqual(post.call_args.kwargs['json']['uuid'], 'stable123')
            first = server.message_db.execute("SELECT first_send_at FROM reply_sends WHERE recording_id='stable123'").fetchone()[0]
            self.assertIsNotNone(first)
            self.assertTrue(server.send_voice_file_to_feishu('ou_test', 'file1', 'stable123'))
            self.assertEqual(first, server.message_db.execute("SELECT first_send_at FROM reply_sends WHERE recording_id='stable123'").fetchone()[0])

    async def test_reply_known_failure_can_be_retried(self):
        server.current_open_id = 'ou_test'
        with patch.object(server, 'upload_and_send_voice_to_feishu', side_effect=[False, True]) as send:
            first = await self.client.post('/api/upload_audio', data=b'voice', headers={'X-Recording-ID': 'retry123'})
            self.assertEqual((await first.json())['status'], 'failed')
            second = await self.client.post('/api/upload_audio', data=b'voice', headers={'X-Recording-ID': 'retry123'})
            self.assertEqual((await second.json())['status'], 'ok')
            self.assertEqual(send.call_count, 2)

    async def test_failed_first_message_is_retried_before_second(self):
        calls = []
        def flaky_tts(text, path):
            calls.append(text)
            if text == 'first' and calls.count('first') == 1:
                raise OSError('temporary')
            fake_tts(text, path)
        with patch.object(server, 'sync_generate_tts', side_effect=flaky_tts), patch.object(server.time, 'sleep'):
            server.on_feishu_message(event('om_fifo1', text='first'))
            server.on_feishu_message(event('om_fifo2', text='second'))
            await self.wait_count(2)
        self.assertEqual(calls, ['first', 'first', 'second'])
        self.assertEqual(server.queue_snapshot()['head_id'], 'om_fifo1')

    async def test_failed_message_notifies_sender_without_enqueue(self):
        import threading
        notified = threading.Event()
        def notify(sender, text):
            self.assertEqual(sender, 'ou_test')
            self.assertIn('重新发送', text)
            notified.set()
        with patch.object(server, 'sync_generate_tts', side_effect=OSError('unavailable')), patch.object(server.time, 'sleep'), patch.object(server, 'send_feishu_text', side_effect=notify):
            server.on_feishu_message(event('om_bad', text='broken'))
            for _ in range(100):
                if notified.is_set(): break
                await asyncio.sleep(.01)
            self.assertTrue(notified.is_set())
        self.assertEqual(server.queue_snapshot()['unread_count'], 0)
        self.assertEqual(server.message_db.execute("SELECT status FROM messages WHERE msg_id='om_bad'").fetchone()[0], 'failed')

    async def test_reply_upload_does_not_create_messages(self):
        server.current_open_id = 'ou_test'
        with patch.object(server, 'upload_and_send_voice_to_feishu', return_value=True):
            response = await self.client.post('/api/upload_audio', data=b'voice')
            self.assertEqual(response.status, 200)
        self.assertEqual(server.queue_snapshot()['unread_count'], 0)
        with self.assertRaises(asyncio.TimeoutError):
            await asyncio.wait_for(self.ws.receive(), 0.1)


class AudioProcessingTest(unittest.TestCase):
    def make_wav(self, path, pcm):
        import array
        samples = array.array('h', pcm)
        if sys.byteorder != 'little':
            samples.byteswap()
        with wave.open(str(path), 'wb') as wav:
            wav.setparams((1, 2, 16000, 0, 'NONE', 'not compressed'))
            wav.writeframes(samples.tobytes())

    def read_wav(self, path):
        with wave.open(str(path), 'rb') as wav:
            return wav.readframes(wav.getnframes())

    def test_trim_only_edges_and_keeps_internal_pause(self):
        path = Path(TEMP.name) / 'trim-test.wav'
        self.make_wav(path, [0] * 8000 + [3000] * 3200 + [0] * 9600 + [3000] * 3200 + [0] * 8000)
        before = self.read_wav(path)
        server.trim_silence(path)
        after = self.read_wav(path)
        self.assertLess(len(after), len(before))
        self.assertIn(b'\x00\x00' * 9600, after)
        self.assertEqual(after.count(b'\xb8\x0b' * 3200), 2)

    def test_adapts_to_steady_background_noise_at_edges(self):
        path = Path(TEMP.name) / 'noisy-edges.wav'
        noise = [400, -400] * 4000
        speech = [5000, -5000] * 4000
        self.make_wav(path, noise + speech + noise)
        before = self.read_wav(path)
        server.trim_silence(path)
        after = self.read_wav(path)
        self.assertLess(len(after), len(before) * 0.9)
        self.assertIn(b'\x88\x13\x78\xec' * 4000, after)

    def test_very_quiet_audio_is_not_erased(self):
        path = Path(TEMP.name) / 'quiet-test.wav'
        self.make_wav(path, [0] * 1600 + [15] * 1600 + [0] * 1600)
        before = self.read_wav(path)
        server.trim_silence(path)
        self.assertEqual(self.read_wav(path), before)

    def test_audio_conversions_produce_readable_output(self):
        path = Path(TEMP.name) / 'input-test.wav'
        out = Path(TEMP.name) / 'output-test.wav'
        opus = Path(TEMP.name) / 'output-test.opus'
        self.make_wav(path, [0] * 8000 + [2000, -2000] * 4000 + [0] * 8000)
        server.convert_to_wav(path, out, denoise=True)
        server.convert_to_opus(path, opus)
        self.assertGreater(len(self.read_wav(out)), 0)
        self.assertGreater(opus.stat().st_size, 0)


if __name__ == '__main__':
    unittest.main()
