````
# tasks.py
import io, uuid, boto3
from celery import shared_task
from openpyxl import Workbook
from django.core.mail import send_mail
from django.conf import settings

s3 = boto3.client("s3")

@shared_task(bind=True, max_retries=3)
def build_and_email_workbook(self, recipient_email, title="Report"):
    # 1) build workbook in memory (same as before)
    wb = Workbook()
    ws = wb.active
    ws.append(["Product", "Units"])
    ws.append(["Widget A", 120])
    buffer = io.BytesIO()
    wb.save(buffer)
    buffer.seek(0)

    # 2) upload to S3 under a unique key
    key = f"reports/{uuid.uuid4()}/{title}.xlsx"
    s3.upload_fileobj(
        buffer,
        settings.AWS_STORAGE_BUCKET_NAME,
        key,
        ExtraArgs={
            "ContentType": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
        },
    )

    # 3) generate a presigned URL that expires (e.g. 1 hour)
    url = s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.AWS_STORAGE_BUCKET_NAME, "Key": key},
        ExpiresIn=3600,   # seconds
    )

    # 4) email just the link — small, reliable
    send_mail(
        subject=f"Your {title} is ready",
        message=f"Download your report (link valid for 1 hour):\n\n{url}",
        from_email=settings.DEFAULT_FROM_EMAIL,
        recipient_list=[recipient_email],
    )
    return f"Uploaded and emailed link for {title}"
