import json
import tempfile
import unittest
import wave
from pathlib import Path
from unittest.mock import patch

import numpy as np

import audio_compress
from recording import RATE


def write_wav(path, seconds=30):
    times = np.arange(int(seconds * RATE)) / RATE
    left = (np.sin(2 * np.pi * 440 * times) * 12000).astype('<i2')
    stereo = np.column_stack((left, np.zeros_like(left)))
    with wave.open(str(path), 'wb') as audio:
        audio.setnchannels(2)
        audio.setsampwidth(2)
        audio.setframerate(RATE)
        audio.writeframes(stereo.tobytes())


def decode(path):
    import av
    with av.open(str(path)) as reader:
        frames = [frame.to_ndarray() for frame in reader.decode(audio=0)]
    return frames


class Base(unittest.TestCase):
    def setUp(self):
        root = tempfile.TemporaryDirectory()
        self.addCleanup(root.cleanup)
        self.root = Path(root.name)
        self.manifest = self.root / 'session.json'
        self.wav = self.root / 'audio-0001.wav'
        self.ogg = self.root / 'audio-0001.ogg'

    def session(self, seconds=5, extra=()):
        write_wav(self.wav, seconds)
        parts = [{'file':self.wav.name, 'start':0, 'duration':seconds}, *extra]
        self.manifest.write_text(json.dumps({'version':2, 'title':'Reunión', 'parts':parts}), encoding='utf-8')

    def files(self):
        return [part['file'] for part in json.loads(self.manifest.read_text(encoding='utf-8'))['parts']]


class CompressFileTest(Base):
    def test_reduces_size_and_keeps_duration(self):
        write_wav(self.wav)
        target = audio_compress.compress_file(self.wav)
        self.assertEqual(target, self.ogg)
        self.assertLess(target.stat().st_size, self.wav.stat().st_size / 10)
        audio_compress.verify(target, 30)
        self.assertTrue(self.wav.is_file())

    def test_channels_stay_separate(self):
        write_wav(self.wav, 5)
        audio_compress.compress_file(self.wav)
        left, right = np.concatenate(decode(self.ogg), axis=1).astype(float)
        # Accepted in CHG-003: at 24 kbps about -26 dB leaks across; each voice must stay on its side.
        leak = 20 * np.log10(max(np.sqrt((right ** 2).mean()), 1e-9) / np.sqrt((left ** 2).mean()))
        self.assertLess(leak, -20)

    def test_bitrate_stays_near_24_kbps(self):
        write_wav(self.wav)
        target = audio_compress.compress_file(self.wav)
        self.assertLess(target.stat().st_size * 8 / 30 / 1000, 30)

    def test_verify_rejects_wrong_duration(self):
        write_wav(self.wav, 5)
        audio_compress.compress_file(self.wav)
        with self.assertRaises(audio_compress.CompressionError):
            audio_compress.verify(self.ogg, 8)

    def test_failed_encode_or_verify_leaves_no_output(self):
        write_wav(self.wav, 1)
        original = self.wav.read_bytes()
        def broken(_, target):
            Path(target).write_bytes(b'incompleto')
            raise OSError('disco')
        with self.assertRaises(OSError):
            audio_compress.compress_file(self.wav, encoder=broken)
        with self.assertRaises(Exception):
            audio_compress.compress_file(self.wav, encoder=lambda _, t: Path(t).write_bytes(b'x'))
        self.assertEqual(sorted(p.name for p in self.root.iterdir()), [self.wav.name])
        self.assertEqual(self.wav.read_bytes(), original)

    def test_corrupt_leftover_ogg_is_replaced(self):
        write_wav(self.wav, 2)
        self.ogg.write_bytes(b'basura')
        audio_compress.compress_file(self.wav)
        audio_compress.verify(self.ogg, 2)


class CompressSessionTest(Base):
    def test_replaces_wav_and_updates_manifest(self):
        self.session()
        report = audio_compress.compress_session(self.manifest)
        self.assertEqual((report['converted'], report['errors']), (1, []))
        self.assertGreater(report['freed'], 0)
        self.assertEqual(self.files(), [self.ogg.name])
        self.assertFalse(self.wav.exists())
        self.assertTrue(self.ogg.exists())
        self.assertEqual(json.loads(self.manifest.read_text(encoding='utf-8'))['title'], 'Reunión')

    def test_failure_keeps_wav_and_manifest(self):
        self.session(1)
        def broken(_, __):
            raise OSError('disco')
        report = audio_compress.compress_session(self.manifest, encoder=broken)
        self.assertEqual(report['errors'], [{'file':self.wav.name, 'error':'OSError'}])
        self.assertEqual(self.files(), [self.wav.name])
        self.assertTrue(self.wav.is_file())

    def test_second_run_skips_converted(self):
        self.session(1)
        audio_compress.compress_session(self.manifest)
        report = audio_compress.compress_session(self.manifest)
        self.assertEqual((report['converted'], report['skipped']), (0, 1))

    def test_one_bad_part_does_not_stop_the_others(self):
        self.session(1, [{'file':'../fuera.wav', 'start':1, 'duration':1}])
        report = audio_compress.compress_session(self.manifest)
        self.assertEqual(report['converted'], 1)
        self.assertEqual(len(report['errors']), 1)

    def test_stop_before_manifest_update_loses_nothing_and_recovers(self):
        self.session(1)
        with patch('audio_compress.atomic_json', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt):
                audio_compress.compress_session(self.manifest)
        self.assertEqual(self.files(), [self.wav.name])
        self.assertTrue(self.wav.is_file())
        report = audio_compress.compress_session(self.manifest)
        self.assertEqual(report['converted'], 1)
        self.assertFalse(self.wav.exists())

    def test_stop_before_wav_delete_keeps_audio_and_recovers(self):
        self.session(1)
        real = Path.unlink
        def stop(path, *args, **kwargs):
            if path == self.wav:
                raise KeyboardInterrupt
            return real(path, *args, **kwargs)
        with patch.object(Path, 'unlink', stop):
            with self.assertRaises(KeyboardInterrupt):
                audio_compress.compress_session(self.manifest)
        self.assertEqual(self.files(), [self.ogg.name])
        self.assertTrue(self.wav.is_file())
        report = audio_compress.compress_session(self.manifest)
        self.assertEqual(report['converted'], 1)
        self.assertFalse(self.wav.exists())

    def test_leftover_wav_with_bad_ogg_is_not_deleted(self):
        self.session(1)
        audio_compress.compress_file(self.wav)
        data = json.loads(self.manifest.read_text(encoding='utf-8'))
        data['parts'][0]['file'] = self.ogg.name
        self.manifest.write_text(json.dumps(data), encoding='utf-8')
        self.ogg.write_bytes(b'basura')
        report = audio_compress.compress_session(self.manifest)
        self.assertEqual(len(report['errors']), 1)
        self.assertTrue(self.wav.is_file())


if __name__ == '__main__':
    unittest.main()
