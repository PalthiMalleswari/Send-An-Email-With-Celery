
import io
from celery import shared_task
from openpyxl import Workbook
import time
from django.core.mail import EmailMessage
from celery.exceptions import SoftTimeLimitExceeded


@shared_task(bind=True,max_retries=3,soft_time_limit=20,time_limit=40,default_retry_delay=10)
def build_and_email_workbook(self,recipient_email, title="Reports"):
    """
    runs in background
    """
    try:


        wb = Workbook()
        ws = wb.active
        ws.title = "Sales"

        ws.append(["Products","Region","Units","Revenue"])

        sample = [
            ["Widget A", "North", 120, 2400],
            ["Widget B", "South", 90, 1800],
            ["Widget C", "East", 200, 5000],
        ]

        for row in sample:
            ws.append(row)

        buffer = io.BytesIO()
        wb.save(buffer)


        buffer.seek(0)
        time.sleep(15)

        email = EmailMessage(
            subject=f"Your {title} is ready",
            body="Hi,\n\n Your report is attached.\n\n Thanks!",
            to=[recipient_email]
        )

        # 3) Attach the workbook bytes

        # email.attach_file("/path/to/report.xlsx")   # reads the file from disk
        
        # Reading file from buffer
        email.attach(
            filename="report.xlsx",                # the name the user sees
            content=buffer.getvalue(),             # the raw bytes of the file
            mimetype="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )

        email.send()

        return f"sent {title}.xlsx to {recipient_email}"

    except SoftTimeLimitExceeded:
        print("Exception Soft Time Limit Exceed")

    except NameError as exc:
        raise self.retry(exc=exc)
    
    except Exception as exc:
        print("Exception Occured",exc)
        raise


@shared_task
def heavy():
    # burn CPU
    total = 0
    for i in range(10_000_000):
        total += i * i
    # eat memory
    big = [0] * 20_000_000        
    time.sleep(20)               
    return total


