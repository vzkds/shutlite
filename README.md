# shutlite

Desligue ou reinicie o PC com Windows pelo celular, sem sair da cama.

O shutlite é um servidor pequeno que roda no PC e abre um painel na rede local.
Do celular você agenda o desligamento, acompanha a contagem regressiva e cancela
se mudar de ideia. É um único arquivo Python, sem dependências e sem Electron.

*A small local-network web panel to shut down or restart a Windows PC from your phone. Python standard library only.*

## O que ele faz

- Agenda desligamento ou reinício: em 15, 30, 60 ou 120 minutos com um toque, em qualquer tempo de 1 a 1440 minutos, ou num horário exato.
- Mostra a contagem regressiva ao vivo em todos os aparelhos conectados.
- Adia em 10 minutos ou cancela a qualquer momento.
- Pareia o celular pelo QR code: o código já leva o PIN.

## Requisitos

- Windows 10 ou 11
- Python 3.9 ou mais novo

## Como usar

Dê dois cliques em `shutlite.bat`, ou rode:

```bash
python server.py
```

Abra `http://localhost:3333` no navegador do PC e aponte a câmera do celular
para o QR code do painel. O celular precisa estar na mesma rede Wi-Fi.

Na primeira execução o shutlite gera um PIN aleatório de 6 dígitos. Para vê-lo:

```bash
python server.py --info
```

Para parar o servidor:

```bash
python server.py --stop
```

Para iniciar junto com o Windows (e para desfazer, use `off`):

```bash
python server.py --autostart on
```

## Opções

| Opção | O que faz |
| --- | --- |
| `--port N` | Porta (padrão 3333; também aceita a variável `PORT`) |
| `--pin XXXX` | Define o PIN (também aceita a variável `PIN`) |
| `--bind IP` | Escuta só nesse endereço |
| `--data-dir PASTA` | Onde ficam `config.json`, `state.json` e o log |
| `--allow-public` | Aceita clientes fora da rede local, como uma VPN |
| `--allow-host NOME` | Aceita mais um nome no cabeçalho `Host` |
| `--dry-run` | Modo simulação: nunca chama o `shutdown.exe` |
| `--debug` | Log detalhado |

Os dados ficam em `%LOCALAPPDATA%\shutlite`. Para trocar o PIN ou a porta de
forma permanente, edite o `config.json` dessa pasta.

## Segurança

O shutlite foi feito para a rede de casa. Ele não usa HTTPS, então não o
exponha à internet nem o use em redes que você não controla.

- Toda ação exige o PIN. Cinco erros seguidos travam o endereço por 5 minutos.
- Só aceita clientes da rede local (faixas privadas), a menos que você use `--allow-public`.
- Recusa requisições vindas de outros sites (checagem de `Origin`, `Host` e `Content-Type`).
- O PIN só é entregue ao navegador do próprio PC, para montar o QR code.
- Se o PC estiver atrás de um proxy reverso local, todo cliente parece ser o próprio PC. Não use o shutlite assim.

## API

Os `POST` recebem JSON com o campo `pin`.

| Rota | Corpo | O que faz |
| --- | --- | --- |
| `GET /api/status` | | Estado atual |
| `GET /api/events` | | Estado ao vivo (Server-Sent Events) |
| `GET /api/info` | | Endereços do painel |
| `POST /api/shutdown` | `seconds` | Agenda o desligamento |
| `POST /api/restart` | `seconds` | Agenda o reinício |
| `POST /api/at` | `at` (`HH:MM`), `action` | Agenda para um horário |
| `POST /api/cancel` | | Cancela |

## Desenvolvimento

```bash
python -m unittest discover -s tests -v
```

Os testes nunca chamam o `shutdown.exe`. Para mexer na interface sem risco de
desligar o PC, rode em modo simulação:

```bash
python server.py --dry-run --bind 127.0.0.1
```

As regras visuais do painel estão em [docs/design-system.md](docs/design-system.md).

## Licença

[MIT](LICENSE). O QR code usa a biblioteca [qrcodejs](https://github.com/davidshimjs/qrcodejs) (MIT), incluída em `web/qrcode.min.js`.
