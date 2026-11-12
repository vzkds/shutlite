# shutlite — interface system

Salvo em 2026-09-24. Direção fechada no rebuild do painel (server.py + web/index.html).

## Direction and feel

Console noturno de bolso: tu na cama, celular na mão, quarto escuro, um toque e o PC
desliga de verdade. Energia de periférico gamer, calma de ritual de sono.
Escuro sempre (sem modo claro — é ferramenta noturna), um acento só.

## Tokens

Trabalhar só com estes; nada de hex avulso no componente.

| Token | Valor | Papel |
| --- | --- | --- |
| `--bg` | `#0b0d10` | página (com glow radial `#18202a` no topo) |
| `--surface` | `#14171c` | cards |
| `--surface-2` | `#1a1f26` | controles neutros |
| `--inset` | `#0e1116` | inputs (afundam — recebem conteúdo) |
| `--border` | `rgba(255,255,255,.09)` | estrutura visível |
| `--border-soft` | `rgba(255,255,255,.06)` | separação sussurrada |
| `--text` | `#f2f4f0` | primário |
| `--secondary` | `#c3cbc2` | suporte |
| `--muted` | `#a7b0a5` | metadados/rótulos (validar contraste no navegador) |
| `--volt` | `#c8ff3d` | ÚNICO acento = agendado/primário; texto sobre ele sempre `--volt-ink` |
| `--danger` | `#ff8585` sobre `--danger-bg` `#2a1518` | só destrutivo (Cancelar) |
| `--ring-track` | `#262c33` | trilho do anel |

## Depth strategy

Bordas-leves, sem sombras dramáticas. Elevação por tom + 1px de borda translúcida.
Glow (`box-shadow` com volt) só no primário e no dot de status. QR usa surface clara
(`#fff`) por necessidade do código — exceção documentada, não padrão.

## Spacing and radius

- Base 4px. Intra-grupo 8px, inter-grupos 16px+.
- Raio concêntrico: card 20px / interno 12px (interno = externo − padding).
- Largura máx 460px, safe-area `env()` no rodapé, sem altura fixa em texto.
- Propriedades lógicas (`inline-size`, `margin-block`) — nada de left/right físico.

## Hierarchy and type

- Herói: anel SVG 200px + timer 34px/700 `tabular-nums` + rótulo de ação.
- H1 20px > rótulos seção 13px/600/tracked uppercase > corpo 14–15px/1.5 > caption 12–13px.
- `text-wrap:balance` em títulos, `pretty` em descrições. Inputs sempre 16px (anti-zoom iOS).
- Peso + cor fazem hierarquia, não só tamanho.

## Component patterns

- Botão primário — `.btn-volt`: 48px mín, raio 12, volt bg + ink `#10130a`, press `scale(.96)`, hover só `brightness(1.06)` em `(hover:hover)`.
- Botão neutro — `.btn-line`: surface-2 + borda; destrutivo — `.btn-danger`: danger-bg + borda 35%.
- Chips de tempo — `.chip`: 56px, um toque agenda (são ações, não seleção); slider `input[type=range]` espelha o campo e o botão repete o valor ("Agendar 45 min").
- Seletor Desligar/Reiniciar — `.seg`: trilho `--inset`, opção ativa em `--surface-2` (`aria-pressed`), raio interno 8px. Neutro de propósito: volt fica para o primário.
- Herói agendado — rótulo com a hora final ("Desliga às 23:42") + `+ 10 min` e `Cancelar`; some quando ocioso.
- PIN — pedido num `<dialog>` só quando uma ação precisa dele; no próprio PC vem do servidor e aparece mascarado ao lado do QR.
- Toast — `#toast > span[role=status]`, some em ~3s; erros de API falam a correção.
- Confirmação destrutiva — `<dialog>` nativo, botão repete consequência ("Desligar").
- Status — `.pill` + `.dot`; ligado = volt sólido, ocioso = cinza sem glow.
- Motion — transições nomeiam propriedade, ≤150ms interativo; `prefers-reduced-motion` zera tudo.

## Notas

- QR via qrcodejs 1.0.0 (MIT) servido localmente em `/qrcode.min.js`, com fallback em texto. Nada é carregado de fora.
- Tempo real via SSE (`GET /api/events`): servidor faz broadcast em todo
  schedule/cancel; página pinta na hora. Polling de `/api/status` é reserva
  (1s até o stream abrir ou se cair, 5s de reconcile) + `rev` monotônico que
  descarta frame antigo. Render é local: `deadline` + intervalo de 500ms
  (`renderLocal`); snapshot só re-ancora. Ações pintam otimista na hora
  (`optimistic`) e o POST confirma; falha faz rollback via `tick().
  Anel usa `stroke-dashoffset` (CIRC=527.8, r=84).
- PIN persiste em `localStorage` (`shutlite-pin`); API exige PIN em todo POST.
- Pareamento: o QR do painel aberto no PC aponta para `http://IP:porta/#pin=…`;
  a página lê o fragmento, salva o PIN e limpa a URL. `GET /api/info` só entrega
  o PIN para loopback.
- Pill tem três estados: Ocioso, Agendado (volt) e Sem conexão (danger).
- Snapshot traz `total`, então o anel continua proporcional depois de recarregar.
