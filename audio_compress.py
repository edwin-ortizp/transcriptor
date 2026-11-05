"""Turn finished sessions' WAV backups into verified Opus files without risking the original."""
import json
import os
import time
import wave
from contextlib import nullcontext
from pathlib import Path

from recording import atomic_json

BITRATE = 24000
OPUS_RATE = 48000
DURATION_TOLERANCE = .5


class CompressionError(Exception):
    pass


def _retry(action):
    # Antivirus and sync clients briefly hold files on Windows, as in recording.atomic_bytes.
    for attempt in range(5):
        try:
            return action()
        except PermissionError as exc:
            if getattr(exc, 'winerror', None) not in (5, 32, 33) or attempt == 4:
                raise
            time.sleep(.05 * (attempt + 1))


def wav_duration(path):
    with wave.open(str(path), 'rb') as audio:
        return audio.getnframes() / audio.getframerate()


def encode(source, target):
    import av
    with av.open(str(source)) as reader, av.open(str(target), 'w', format='ogg') as writer:
        # Default VBR measured at twice the requested rate; constrained VBR keeps the size predictable.
        output = writer.add_stream('libopus', rate=OPUS_RATE, layout='stereo',
                                   options={'b':str(BITRATE), 'vbr':'constrained'})
        resampler = av.AudioResampler(format='s16', layout='stereo', rate=OPUS_RATE)
        for frame in reader.decode(audio=0):
            for converted in resampler.resample(frame):
                for packet in output.encode(converted):
                    writer.mux(packet)
        for converted in resampler.resample(None):
            for packet in output.encode(converted):
                writer.mux(packet)
        for packet in output.encode(None):
            writer.mux(packet)


def verify(path, expected):
    """Decode the whole file: a readable header alone does not prove the audio is complete."""
    import av
    decoded = 0
    with av.open(str(path)) as reader:
        stream = reader.streams.audio[0]
        if stream.codec_context.name not in ('opus', 'libopus') or stream.codec_context.channels != 2:
            raise CompressionError('El audio comprimido no tiene el formato esperado')
        for frame in reader.decode(stream):
            decoded += frame.samples / frame.sample_rate
    if abs(decoded - expected) > DURATION_TOLERANCE:
        raise CompressionError('El audio comprimido no dura lo mismo que el original')


def _fsync(path):
    with open(path, 'r+b') as file:
        os.fsync(file.fileno())


def compress_file(source, encoder=encode, verifier=verify):
    """Create a verified .ogg beside the WAV. The WAV is never modified here."""
    source = Path(source)
    target = source.with_suffix('.ogg')
    temporary = source.with_suffix('.ogg.tmp')
    expected = wav_duration(source)
    if target.is_file():
        # A previous run stopped after renaming: reuse the file only if it proves valid.
        try:
            verifier(target, expected)
            return target
        except Exception:
            _retry(target.unlink)
    try:
        encoder(source, temporary)
        verifier(temporary, expected)
        _fsync(temporary)
        _retry(lambda: temporary.replace(target))
    finally:
        if temporary.exists():
            _retry(temporary.unlink)
    return target


def compress_session(manifest, *, lock=None, encoder=encode, verifier=verify, on_renamed=None):
    """Order per part: valid Opus -> manifest points to it -> WAV removed.
    A stop between any two steps leaves playable audio, and running again finishes the job.
    `on_renamed(old, new)` runs inside `lock`, so in-memory copies of the parts stay in step."""
    manifest = Path(manifest)
    guard = lock or nullcontext()
    report = {'converted':0, 'skipped':0, 'freed':0, 'errors':[]}
    with guard:
        names = [part['file'] for part in _read(manifest)['parts']]
    for name in names:
        try:
            freed = _compress_part(manifest, name, guard, encoder, verifier, on_renamed)
        except Exception as exc:
            report['errors'].append({'file':name, 'error':type(exc).__name__})
            continue
        if freed is None:
            report['skipped'] += 1
        else:
            report['converted'] += 1
            report['freed'] += freed
    return report


def _read(manifest):
    return json.loads(manifest.read_text(encoding='utf-8'))


def _compress_part(manifest, name, guard, encoder, verifier, on_renamed):
    root = manifest.parent
    path = root / name
    if Path(name).name != name:
        raise CompressionError('Referencia de audio inválida')
    if name.endswith('.ogg'):
        # The manifest moved on but the WAV survived a stop: delete it only after re-checking.
        wav = path.with_suffix('.wav')
        if not wav.is_file():
            return None
        verifier(path, wav_duration(wav))
        size = wav.stat().st_size
        _retry(wav.unlink)
        return size
    if not name.endswith('.wav'):
        raise CompressionError('Referencia de audio inválida')
    if not path.is_file() or wav_duration(path) == 0:
        return None
    target = compress_file(path, encoder, verifier)
    size = path.stat().st_size
    with guard:
        if on_renamed:
            on_renamed(name, target.name)
        data = _read(manifest)
        for part in data['parts']:
            if part['file'] == name:
                part['file'] = target.name
        atomic_json(manifest, data)
    _retry(path.unlink)
    return size - target.stat().st_size
