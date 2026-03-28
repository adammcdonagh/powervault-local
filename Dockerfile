FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .

# Default entrypoint runs the controller.
# To run the hardware poll utility instead, override the command:
#   docker run --rm -it ... powervault-local python poll.py --battery /dev/ttyUSB1
CMD ["python", "-m", "controller.main"]
