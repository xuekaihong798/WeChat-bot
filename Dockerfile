# WeChat-bot Dockerfile
FROM python:3.12-slim

WORKDIR /app

COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

COPY bot.py backend.py config.py clawbot.py skill_loader.py ./
COPY skills/ ./skills/

EXPOSE 8765

CMD ["python", "bot.py", "--server"]
