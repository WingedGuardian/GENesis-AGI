FROM python:3.12-slim-bookworm@sha256:6a0e06f5ba0b373d051aabd3fffb9b873f27f9a2a3a14bcbe37dee6f0e3bbc70
RUN apt-get update && apt-get install -y --no-install-recommends bash jq curl util-linux procps coreutils findutils gawk git && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir claude-agent-sdk==0.2.172
COPY headlong/ /headlong/
COPY headlong_fixture.py headlong_controls.py /fixture/
RUN chmod 755 /fixture/headlong_fixture.py && ln -s headlong_fixture.py /fixture/fake-claude && dpkg-query -W > /fixture/packages.txt && pip freeze > /fixture/python-packages.txt
ENV HOME=/work/home PYTHONDONTWRITEBYTECODE=1
USER 65532:65532
WORKDIR /work
CMD ["python3", "/fixture/headlong_controls.py"]
