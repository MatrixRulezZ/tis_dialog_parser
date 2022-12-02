FROM python:3.7 as builder
COPY ./requirements.txt /requirements.txt
USER root


RUN pip install --user -r requirements.txt

# second unamed stage
FROM python:3.7-slim
WORKDIR /
COPY --from=builder /root/.local /root/.local
COPY . /
EXPOSE 8080
ENV PATH=/root/.local:$PATH

ENV LD_LIBRARY_PATH=/lib64:$LD_LIBRARY_PATH

RUN apt-get update && apt-get install -y  \
  libaio1 \
    build-essential \
  && rm -rf /var/lib/apt/lists/*

CMD ["python", "-u", "./main.py"]