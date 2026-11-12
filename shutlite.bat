@echo off
setlocal
cd /d "%~dp0"

if /i "%~1"=="stop" (
  python server.py --stop
  goto :eof
)

rem mostra enderecos e PIN (o PIN e gerado na primeira execucao)
python server.py --info
if errorlevel 1 goto :eof

rem sobe o servidor sem janela; erros vao para %LOCALAPPDATA%\shutlite\shutlite.log
start "" pythonw "%~dp0server.py"

echo.
echo shutlite rodando. Abra o endereco acima no navegador deste PC
echo e aponte a camera do celular para o QR do painel.
echo Para parar: shutlite.bat stop
pause
