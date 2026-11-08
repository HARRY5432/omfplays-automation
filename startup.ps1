# omfplays startup processor — runs at Windows login
$ErrorActionPreference = "Continue"
$env:PIPELINE_NODE = "local"
cd "F:\✈Content Engine\omfplays"
python run_local_pipeline.py
