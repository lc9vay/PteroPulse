FROM python:3.13-slim

WORKDIR /app
COPY monitor.py .

# Render 会注入 PORT(默认 10000),脚本自动读取
ENV PORT=10000

EXPOSE 10000

CMD ["python", "monitor.py"]
