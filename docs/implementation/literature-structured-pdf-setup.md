# Optional local PDF extraction

Install Docling 2.150.1 (MIT) into a separate local Python environment:

```bash
python3 -m venv /path/to/docling-venv
/path/to/docling-venv/bin/pip install -r backend/requirements-docling.txt
/path/to/docling-venv/bin/docling-tools models download --output-dir /path/to/docling-models
```

Set `JARVISOS_DOCLING_PYTHON` to that environment's `bin/python` and `JARVISOS_DOCLING_ARTIFACTS` to the prefetched model directory before starting the backend. The layout and table model bundle is about 669 MB; the isolated environment is about 6.2 GB. The service supplies `HF_HUB_OFFLINE=1`, `TRANSFORMERS_OFFLINE=1`, and an explicit artifacts path to each converter process. Extraction is requested from a registered PDF and uses only local bytes. No model is downloaded by a product request. If the interpreter or bundle is absent, the extraction route reports unavailable.
