@echo off
REM Royal Render Execute-app entry point for a ComfyUI job.
REM
REM RR's Execute app invokes the scene file directly with three positional
REM args: SeqStart SeqEnd SeqStep, followed by AdditionalCommandlineParam and
REM any CustomFlags from the job's submission. This batch file is that scene
REM file: it forwards those args to the Python wrapper, which does the real
REM work (see comfyui_execute.py). Exit code is passed through unchanged;
REM RR's Execute app checks it directly (<CheckExitCode> in its config).
REM
REM PYTHON must point at the interpreter that has ComfyUI's own dependencies
REM importable if the wrapper needs any (currently it uses only the stdlib).
REM Default is the fleet venv interpreter (NODE-SETUP-QUICKSTART step 1): bare
REM "python" on these boxes is a Microsoft Store stub that silently does
REM nothing. Set PYTHON once per node if its interpreter lives elsewhere.

if "%PYTHON%"=="" set PYTHON=C:\genvideo\venv\Scripts\python.exe

"%PYTHON%" "%~dp0comfyui_execute.py" %*
exit /b %ERRORLEVEL%
