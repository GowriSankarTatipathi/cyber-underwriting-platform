from django.conf import settings
from django.core.files.uploadhandler import FileUploadHandler, StopUpload


class SizeLimitedUploadHandler(FileUploadHandler):
    """Limit multipart file bytes before they can fill the temporary disk."""
    def new_file(self, *args, **kwargs):
        super().new_file(*args, **kwargs)
        self.received = 0
        if self.content_length and self.content_length > settings.EVIDENCE_MAX_BYTES:
            self.request.evidence_upload_too_large = True
            raise StopUpload(connection_reset=True)

    def receive_data_chunk(self, raw_data, start):
        self.received += len(raw_data)
        if self.received > settings.EVIDENCE_MAX_BYTES:
            self.request.evidence_upload_too_large = True
            raise StopUpload(connection_reset=True)
        return raw_data

    def file_complete(self, file_size):
        return None
