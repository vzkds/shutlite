<p align="center">
  <picture>
    <source media="(prefers-color-scheme: dark)" srcset="Brand/shutlite-logo-dark.svg">
    <img src="Brand/shutlite-logo-light.svg" alt="shutlite" width="320">
  </picture>
</p>

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

Para saber se ele está rodando (sai com código 0 se sim, 1 se não):

```bash
python server.py --check
```

Para iniciar junto com o Windows (e para desfazer, use `off`):

```bash
python server.py --autostart on
```

## Opções

| Opção | O que faz |
| --- | --- |
| `--port N` | Porta (padrão 3333; também aceita a variável `PORT`) |
| `--pin XXXXXX` | Define o PIN, com 6 ou mais caracteres (também aceita a variável `PIN`) |
| `--bind IP` | Escuta só nesse endereço |
| `--data-dir PASTA` | Onde ficam `config.json`, `state.json` e o log |
| `--allow-public` | Aceita clientes fora da rede local, como uma VPN |
| `--allow-host NOME` | Aceita mais um nome no cabeçalho `Host` |
| `--dry-run` | Modo simulação: nunca chama o `shutdown.exe` |
| `--windows-timer` | Volta ao timer do Windows desde o início, com o aviso dele (ver "Modo silencioso"). Também vale `"windows_timer": true` no `config.json` |
| `--debug` | Log detalhado |

Os dados ficam em `%LOCALAPPDATA%\shutlite`. Para trocar o PIN ou a porta de
forma permanente, edite o `config.json` dessa pasta. O PIN precisa ter 6 ou mais
caracteres; com um PIN menor o servidor não inicia e diz como corrigir.

### Modo silencioso (padrão)

Por padrão o shutlite conta o tempo sozinho e **não** chama o Windows ao agendar,
adiar ou cancelar. Assim o Windows não mostra o aviso de desligamento ("você será
desconectado…") a cada toque no painel. O `shutdown.exe` só é chamado uma vez,
quando faltam 60 segundos: o aviso do Windows nesse último minuto é intencional,
é o último alerta antes de desligar. Um agendamento de 60 s ou menos (como
"Desligar agora") vai ao Windows na hora. Depois dessa entrega o Windows é o
dono do timer, e cancelar chama `shutdown /a` (o Windows pode avisar que foi
cancelado).

**Risco: se o shutlite não estiver rodando no horário, o PC não desliga.** Ele
precisa estar aberto até o fim da contagem. Use `python server.py --autostart on`
para ele subir junto com o Windows. Se ele for fechado e aberto de novo antes do
prazo, a contagem continua.

Parar o shutlite de forma ordenada (`python server.py --stop`, Ctrl+C na janela
dele, ou o botão de parar do painel) com um desligamento pendente **entrega o que
falta ao Windows** antes de fechar, e avisa: "Há um desligamento às HH:MM; entreguei
ao Windows (o aviso dele vai aparecer)." Se a entrega falhar, o aviso diz que o
desligamento **não** vai acontecer. Isso não vale quando o shutlite é encerrado por
fora: matar o processo (Gerenciador de Tarefas, `taskkill`), fazer logoff ou desligar
o PC, ou um antivírus que o feche. Nesses casos nada é entregue ao Windows, a
contagem só no shutlite para em silêncio e o PC não desliga (a contagem fica salva:
se o shutlite voltar a rodar antes do prazo, ela continua). Se o prazo passar
enquanto ele ou o PC estavam desligados ou suspensos (dormindo), o agendamento é
descartado e nada é desligado fora de hora; isso fica registrado em `shutlite.log`.

O shutlite não confia só no relógio do Windows: ele guarda o prazo também em um
relógio interno que não sofre ajuste de hora. Se os dois discordarem em mais de
5 segundos (alguém mudou a hora do PC, a sincronização de hora deu um salto, ou o PC
foi suspenso durante a contagem), ele descarta o agendamento em vez de arriscar
desligar cedo ou tarde, e registra o motivo em `shutlite.log`. Nada é desligado
nesse caso: agende de novo.

Para voltar ao comportamento antigo (tudo pelo timer do Windows, com o aviso desde
o início) use `python server.py --windows-timer` ou ponha `"windows_timer": true`
no `config.json`. A segunda forma também vale para o `--autostart on`, que não
repassa opções.

### Se o PC reiniciar

Depende de quem tem o timer:

- **Contagem só no shutlite** (modo silencioso, antes dos últimos 60 s): ela
  sobrevive a um reinício do PC se o shutlite abrir de novo (use
  `--autostart on`). Só o prazo vale: se ele já tiver passado quando o shutlite
  voltar, o agendamento é descartado e nada é desligado.
- **Timer já no Windows** (com `--windows-timer`, ou depois da entrega dos últimos
  60 s): o timer vive no Windows e some quando o PC reinicia. Por isso o shutlite
  guarda a hora em que o Windows iniciou junto com o agendamento e, ao abrir de
  novo, descarta o agendamento se o PC reiniciou desde então. Assim o painel não
  mostra uma contagem que nunca vai acontecer.

Reiniciar só o shutlite mantém o agendamento nos dois casos.

Limite: com a Inicialização Rápida do Windows ligada, desligar e ligar o PC pode
não ser percebido como reinício (o contador de tempo ligado do Windows pode
continuar). Nesse caso, com o timer já no Windows, o painel pode mostrar um
agendamento que já não existe. Cancele pelo painel ou desative a Inicialização
Rápida se isso importar para você.

## Segurança

O shutlite foi feito para a rede de casa. Ele não usa HTTPS, então não o
exponha à internet nem o use em redes que você não controla.

- Toda ação exige o PIN. Cinco erros seguidos travam o endereço por 5 minutos (no IPv6, o bloco /64 inteiro).
- Além disso, 20 erros em 10 minutos somando todos os aparelhos da rede travam todos eles por 5 minutos; a cada
  nova reincidência o tempo dobra, até 1 hora. O próprio PC (`localhost`) não entra nesse bloqueio geral,
  então `--stop` e o painel local continuam funcionando durante um ataque vindo da rede.
- Cada PIN errado fica registrado em `shutlite.log`, com o endereço de quem errou.
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

O estado (`/api/status`, `/api/events` e a resposta dos `POST`) traz `scheduled`,
`left`, `total`, `action`, `rev` e `mode`: `"local"` (contagem só no shutlite, antes
da entrega), `"windows"` (o Windows já tem o timer, ou modo `--windows-timer`) ou
`null` (nada agendado).

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
