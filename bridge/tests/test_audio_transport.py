import io
import unittest

from app.audio_transport import audio_chunks


class Body(io.BytesIO):
    def __init__(self, body, start, end, total, etag='"same"'):
        super().__init__(body)
        self.status = 206
        self.headers = {"Content-Range": f"bytes {start}-{end}/{total}",
                        "Content-Length": str(end - start + 1), "ETag": etag}


class AudioTransportTests(unittest.TestCase):
    def test_truncated_body_resumes_exactly_once_without_duplicate_bytes(self):
        payload = bytes(range(256)) * 1024
        first = Body(payload[:71003], 0, len(payload) - 1, len(payload))
        offsets = []
        def reopen(start, end):
            offsets.append(start)
            return Body(payload[start:end + 1], start, end, len(payload))
        result = b"".join(audio_chunks(first, reopen))
        self.assertEqual(result, payload)
        self.assertEqual(offsets, [71003])
        self.assertTrue(first.closed)

    def test_seek_recovery_preserves_requested_window(self):
        first = Body(b"abcd", 10, 19, 100)
        def reopen(start, end):
            self.assertEqual((start, end), (14, 19))
            return Body(b"efghij", 14, 19, 100)
        self.assertEqual(b"".join(audio_chunks(first, reopen)), b"abcdefghij")

    def test_wrong_range_or_changed_audio_is_never_spliced(self):
        for wrong in (Body(b"abcdefghij", 0, 9, 10), Body(b"efghij", 4, 9, 10, '"changed"')):
            with self.subTest(headers=wrong.headers):
                first = Body(b"abcd", 0, 9, 10)
                with self.assertRaises(OSError):
                    b"".join(audio_chunks(first, lambda start, end: wrong, max_retries=1))

    def test_cancel_closes_upstream(self):
        first = Body(b"a" * 131072, 0, 131071, 131072)
        iterator = audio_chunks(first, lambda *_: self.fail("unexpected retry"))
        next(iterator)
        iterator.close()
        self.assertTrue(first.closed)


if __name__ == "__main__":
    unittest.main()
