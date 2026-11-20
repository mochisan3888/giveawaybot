FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt && useradd --uid 10001 --create-home bot && mkdir /app/data && chown bot:bot /app/data
COPY giveaway ./giveaway
USER bot
CMD ["python", "-m", "giveaway.bot"]
