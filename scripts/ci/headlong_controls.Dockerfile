FROM python:3.12-slim-trixie@sha256:a0f57064cbdcd6285fa45f51237ab0b35ea8ebaed06a1f5973e89e19dfb86704
# Pinned recap uses $end; jq 1.6 rejects it. Check the measured compiler seam.
RUN apt-get update && apt-get install -y --no-install-recommends bash jq curl util-linux procps coreutils findutils gawk git && rm -rf /var/lib/apt/lists/* && jq -n --argjson end 1 '$end'
RUN pip install --no-cache-dir claude-agent-sdk==0.2.172 aiosqlite==0.22.1
COPY headlong/ /headlong/
COPY src/ /genesis-src/
COPY LICENSE /genesis-source-pin /fixture/
COPY headlong_fixture.py headlong_controls.py headlong_native_controls.py headlong_native_provider.py headlong_native_bridge.py headlong_monolith_controls.py headlong_bridge_patch.json /fixture/
COPY headlong_prompt_references.py headlong_reference_patch.py headlong_reference_controls.py /fixture/
RUN cp -a /headlong /headlong-b && python3 /fixture/headlong_reference_patch.py /headlong-b
RUN python3 -c 'import json, sys; from pathlib import Path; sys.path.insert(0,"/fixture"); from headlong_reference_patch import replace_once; p=Path("/headlong-b/thinkers/monolith/step"); spec=json.loads(Path("/fixture/headlong_bridge_patch.json").read_text()); s=replace_once(p.read_text(),spec["before"],spec["after"]); s=replace_once(s,spec["prompt_before"],spec["prompt_after"]); p.write_text(s)'
RUN chmod 755 /fixture/headlong_fixture.py /fixture/headlong_native_bridge.py && ln -s headlong_fixture.py /fixture/fake-claude && dpkg-query -W > /fixture/packages.txt && pip freeze > /fixture/python-packages.txt
ENV HOME=/work/home PYTHONDONTWRITEBYTECODE=1 PYTHONPATH=/genesis-src
USER 65532:65532
WORKDIR /work
CMD ["python3", "/fixture/headlong_controls.py"]
