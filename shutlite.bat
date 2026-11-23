@echo off
setlocal
rem nao deixa o cmd achar python.exe/.cmd plantado na pasta atual (so o PATH vale)
set "NoDefaultCurrentDirectoryInExePath=1"
cd /d "%~dp0"

rem ---- acha o Python 3.9+: primeiro "python", depois o launcher "py -3".
rem O atalho da Microsoft Store (python sem instalar) falha no teste e e ignorado.
set "PY="
set "PYW="
python -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1 && (set "PY=python" & set "PYW=pythonw")
if not defined PY py -3 -c "import sys; sys.exit(sys.version_info < (3, 9))" >nul 2>&1 && (set "PY=py -3" & set "PYW=pyw -3")
if not defined PY goto :nopython

if /i "%~1"=="stop" goto :stop

rem ---- mostra enderecos e PIN (o PIN e gerado na primeira execucao)
%PY% server.py --info
if %errorlevel% neq 0 goto :infofail

rem ---- sobe o servidor sem janela; erros vao para shutlite.log na pasta de dados
start "" %PYW% "%~dp0server.py"

rem ---- espera ate ~8 s o servidor responder; so avisa "rodando" se responder
set "UP="
for /l %%i in (1,1,8) do if not defined UP (
  ping -n 2 127.0.0.1 >nul
  %PY% server.py --check >nul 2>&1 && set "UP=1"
)
if not defined UP goto :notup

echo.
echo shutlite rodando. Abra o endereco acima no navegador deste PC
echo e aponte a camera do celular para o QR do painel.
echo Para parar: shutlite.bat stop
pause
exit /b 0

:stop
%PY% server.py --stop
exit /b %errorlevel%

:nopython
echo.
echo Python 3.9 ou mais novo nao foi encontrado neste PC.
echo Instale em https://www.python.org/downloads/ e marque "Add python.exe to PATH".
echo.
pause
exit /b 1

:infofail
echo.
echo Nao foi possivel ler a configuracao do shutlite. Veja a mensagem acima.
echo.
pause
exit /b 1

:notup
echo.
echo O shutlite nao respondeu depois de 8 segundos.
%PY% server.py --check
echo.
echo Se a porta estiver em uso, feche o outro programa ou mude a porta no config.json.
echo.
pause
exit /b 1
