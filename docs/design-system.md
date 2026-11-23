# shutlite — interface system

Redesign de 2026-11-12 (painel `web/index.html`, um arquivo só). Referência visual: getartcraft.com
(captura em `Shared/shutlite/referencia/getartcraft-desktop.png`). Backup do estado anterior em
`Shared/shutlite/backup-pre-redesign/`.

## Direction and feel

Instrumento técnico e calmo. Fundo off-white quente (ou preto quente), tinta quase preta, cantos RETOS,
linhas de 1px no lugar de sombras, rótulos em mono maiúsculo, título largo e pesado, muito espaço.
A contagem regressiva é o centro: um anel de progresso sobre órbitas concêntricas finas.

- **Preto é tema de primeira classe**, não variação: fundo `#0B0B0C` (o preto da referência), mesmas regras de raio 0, bordas finas e órbitas em tons claros de baixo contraste (`--orbit` `#3A3A3F`, arco `#F2F1EE`). Todo token existe nos dois temas e passa na mesma tabela de contraste; as capturas cobrem os dois temas em todos os estados.
- **Tema** (claro ↔ preto): segue `prefers-color-scheme` por padrão (a ferramenta é usada à noite; o claro não pode ofuscar
  quem abre no escuro) e tem botão claro/escuro na barra, lembrado em `localStorage` (`shutlite-theme`,
  via wrapper `store`). Se o storage estiver bloqueado, vale só a preferência do sistema. Um script
  inline no `<head>` aplica o tema salvo antes da primeira pintura. `<meta name="color-scheme">` vale
  `light dark` sem escolha salva e `light`/`dark` com escolha; `<meta name="theme-color">` acompanha o tema
  ativo (`#F2F1EE` / `#0B0B0C`), no head e a cada troca (`applyTheme`). **Isto revoga a regra antiga
  "escuro sempre".**
- **Um acento só**: azul, para o estado "agendado" (anel, pill ativa, foco, slider). O verde-limão antigo saiu.
- **Raio 0 em tudo** (`*{border-radius:0}`; token `--r:0`): botões, inputs, cards, diálogos, toast, pill.
- Sem fontes externas e sem arquivos novos: o servidor só serve `/` e `/qrcode.min.js` e o empacotamento
  quer um painel num arquivo só. Pilhas de sistema (abaixo); marca desenhada em SVG inline.

## Tokens

Trabalhar só com estes; nada de hex avulso no componente (únicas exceções: `theme-color`, que o JS
mantém em sincronia com o tema, e o favicon — atributos HTML não aceitam `var()`).

| Token | Claro | Escuro | Papel |
| --- | --- | --- | --- |
| `--bg` | `#F2F1EE` | `#0B0B0C` | página, barra, rodapé |
| `--surface` | `#F8F7F4` | `#131315` | colunas laterais, cards (mobile), diálogos |
| `--surface-2` | `#E8E7E3` | `#1B1B1E` | hover de controles |
| `--inset` | `#FFFFFF` | `#0B0B0C` | inputs (recebem conteúdo) |
| `--line` | `#C9C8C4` | `#2A2A2E` | divisórias decorativas (barra, colunas, rodapé) |
| `--border` | `#686767` | `#8A8A90` | borda de controle e trilho do anel (≥4,5:1) |
| `--text` | `#101014` | `#F2F1EE` | primário |
| `--secondary` | `#46464A` | `#C2C2C4` | suporte |
| `--muted` | `#5E5E62` | `#9C9C9F` | rótulos mono, metadados |
| `--btn` / `--on-btn` | `#101014` / `#F2F1EE` | `#F2F1EE` / `#0B0B0C` | botão primário sólido e seg ativo |
| `--accent` / `--on-accent` | `#2F5BE0` / `#FFFFFF` | `#7C9CFF` / `#0B0B0C` | agendado: anel, pill, foco, slider |
| `--logo-sq` | `#2F5BE0` | `#4E7BFB` | quadrado do logo (ver "Identidade") |
| `--danger` | `#B3261E` | `#FF8A84` | só destrutivo (Cancelar), contorno + texto, sem preenchimento |
| `--orbit` / `--orbit-strong` | `#B4B3AF` / `#101014` | `#3A3A3F` / `#F2F1EE` | órbitas de fundo (linha / arco) |
| `--toast` / `--on-toast` | `#101014` / `#F2F1EE` | `#F2F1EE` / `#0B0B0C` | toast invertido |
| `--dot-idle` | `#686767` | `#8A8A90` | dot de status ocioso |
| `--bg-glow` | — | — | (removido: sem glow) |
| `--qr-bg` / `--qr-ink` / `--qr-muted` | `#fff` / `#111` / `#555` | iguais | QR precisa de fundo claro nos dois temas (exceção documentada) |

### Contraste medido (WCAG 2.x, fórmula de luminância relativa)

| Par | Claro | Escuro | Mínimo |
| --- | --- | --- | --- |
| `--text` sobre `--bg` | 16,81 | 17,42 | 4,5 |
| `--secondary` sobre `--bg` | 8,32 | 11,06 | 4,5 |
| `--muted` sobre `--bg` / `--surface` | 5,71 / 6,03 | 7,19 / 6,78 | 4,5 |
| `--accent` sobre `--bg` (anel, foco, texto) | 5,03 | 7,55 | 3 (gráfico) / 4,5 (texto) |
| `--on-accent` sobre `--accent` (pill ativa) | 5,69 | 7,55 | 4,5 |
| `--danger` sobre `--bg` / `--surface` | 5,79 / 6,10 | 8,64 / 8,15 | 4,5 |
| `--border` (controles e trilho do anel) sobre `--bg` / `--surface` / `--inset` | 4,99 / 5,26 / 5,64 | 5,73 / 5,41 / 5,73 | 4,5 (exigência do projeto; WCAG pede 3) |
| `--text` sobre `--surface-2` (hover) / `--accent` sobre `--surface-2` | 7,59 (secundário) / 4,59 | 9,66 (secundário) / 6,59 | 4,5 |
| `--qr-ink` / `--qr-muted` sobre `--qr-bg` (`#fff`) | 18,88 / 7,46 | 18,88 / 7,46 | 4,5 |

**Auditoria no painel real** (script que lê `getComputedStyle` de todo texto visível e de toda borda de botão/input, com o fundo efetivo): 53 verificações por tema, mínimo 4,99 (claro) e 5,41 (preto), 0 falhas. Visão remota (campo de PIN, Salvar, dica, rótulo, placeholder, "PIN salvo"): mínimo 5,41, 0 falhas. O que fica de fora de propósito: linhas decorativas (`--line`, órbitas) e controles desabilitados. Desabilitado usa `opacity:.45`, isento de contraste (WCAG 1.4.3).
| `--on-btn` sobre `--btn` | 16,81 | 17,42 | 4,5 |

Medido, não estimado: os valores da referência (`#4E7BFB` acento, `#666667` secundário, `#989797`
borda) foram trocados porque `#4E7BFB` dá só 3,35 sobre `#F2F1EE`, `#989797` dá 2,58 como borda e
`#666667` dá 3,43 sobre `#0B0B0C`. Texto sobre o QR: `#555` sobre `#fff` = 7,46.

### Tipografia

- Título/marca: o wordmark SHUTLITE em curvas (SVG inline, ver "Identidade") — é ele que dá o título largo e pesado
  da referência, sem fonte externa. Relógio: `system-ui` peso 900, `font-stretch:125%` (só efeito em fontes
  variáveis; inofensivo nas demais), `tabular-nums`.
- Rótulos, botões, chips e metadados: `--mono` = `ui-monospace, "Cascadia Mono", "Geist Mono", Consolas, "SF Mono", Menlo, monospace`,
  11–12px, maiúsculo, `letter-spacing:.08–.12em`.
- Corpo: `--font` = `system-ui, -apple-system, "Segoe UI", Roboto, Inter, Arial, sans-serif`, 13–15px/1.5.
  Inputs sempre 16px (anti-zoom iOS). `text-wrap:balance` em títulos, `pretty` em descrições.

## Identidade

Conceito **"Deadline"**: o **anel aberto** é o prazo (a contagem que se esgota), o **anel interno** é o relógio,
e o **quadrado azul** no vão é o instante do limite — o momento em que o PC desliga. Geometria quadrada
combina com os cantos retos do sistema; o azul é o mesmo acento do estado "agendado".

Arquivos oficiais em `Brand/` (não editar; a página não os carrega — o painel é um arquivo só e o servidor só
serve `/` e `/qrcode.min.js`, então as formas são copiadas inline):

| Arquivo | Uso |
| --- | --- |
| `shutlite-logo-{light,dark}.svg` (408×84, símbolo + nome) | README (`<picture>` por `prefers-color-scheme`); fonte das formas da barra |
| `shutlite-symbol-{light,dark}.svg` (32×32) | referência do símbolo; base do favicon próprio |
| `shutlite-wordmark-{light,dark}.svg` (viewBox `-4 -4 312 44`) | só o nome, quando o símbolo estiver em outro lugar; mesmas curvas da barra |

- **Versões**: `light` = tinta `#101014`, para fundos claros; `dark` = tinta `#F2F1EE`, para fundos pretos. No painel
  não há duas versões: o traço e as letras usam `var(--text)` (que vale exatamente esses dois valores) e o
  tema troca sozinho.
- **Quadrado**: no arquivo oficial é `#4E7BFB`. No painel usa o token `--logo-sq`: **`#2F5BE0` no claro**
  (5,03:1 sobre `#F2F1EE`; o `#4E7BFB` dá só 3,35) e **`#4E7BFB` oficial no preto** (5,20:1 sobre `#0B0B0C`).
  Em material externo (README, apresentações) vale o `#4E7BFB` dos arquivos.
- **Área de respiro**: mínimo de 1/2 da altura do símbolo em volta do logo; na barra o respiro é o
  `padding-inline:16px` da célula (12px a 360px) + as linhas de 1px.
- **Tamanho mínimo**: logo completo ≥ 22px de altura (≈107px de largura; abaixo o nome perde legibilidade);
  só o símbolo ≥ 16px. Na barra: 28px no desktop, 24px no mobile, 22px a ≤360px.
- **Acessibilidade**: `<h1 class="logo-h">` com `<svg role="img" aria-label="shutlite">`; o nome do cabeçalho é "shutlite".
- **Favicon próprio** (data-URI no `<head>`, SVG): o símbolo com traço mais grosso (3,2 e 3 em vez de 1,75) e o
  quadrado maior (4,2 em vez de 2,85), porque em 16px o desenho original vira linha de 0,9px e quadrado de 1,4px.
  Sem fundo; `@media (prefers-color-scheme: dark)` dentro do SVG troca `#101014` ↔ `#F2F1EE` (a aba do navegador
  pode ser clara ou escura, independente do tema do painel); o quadrado é o `#4E7BFB` oficial nos dois casos
  (3,8:1 em aba branca, 4,3:1 em aba escura). Atenção ao escapar: `#` vira `%23`, `<`/`>` viram `%3C`/`%3E`.
- **Onde NÃO usar**: o herói ocioso fica só com o anel de progresso (o símbolo ali competiria com o contador); não há
  mais ícone de power no painel.

## Layout

### Desktop — largura ≥ 900px **e** altura ≥ 600px

`html,body{overflow:hidden}`, `.app{height:100dvh}` (fallback `100vh`), grade
`barra 48px / palco 1fr / rodapé fino`. **Sem scroll de página.**

- **Barra** (células separadas por linha de 1px, como na referência): logo (símbolo + wordmark, SVG inline) |
  dot + pill `role="status"` | nota de simulação (`#sub`) | versão (`/api/info`) | botão de tema (48×48).
  Não há botão sólido no fim da barra (a referência tem "Launch app"; aqui ele pareceria uma ação).
- **Palco**: grade de 3 colunas `clamp(272px,25vw,440px) 1fr clamp(272px,25vw,440px)`.
  Esquerda = **Agendar** (seg Desligar/Reiniciar, chips, minutos+slider, horário exato — grupos
  espalhados pela altura, controles crescem com `vh`). Centro = **herói**: órbitas SVG
  (decorativas, `aria-hidden`) atrás do anel de progresso (o contador de verdade), rótulo da ação e
  `+ 10 min` / `Cancelar`. Direita = **Agora** (desligar/reiniciar agora, Cancelar tudo) e **Acesso**
  (QR + PIN no PC / campo de PIN no celular).
- Tamanhos com `clamp()`/`vh`: anel `clamp(220px,min(34vw,54vh),640px)`, QR `clamp(104px,15vh,150px)`
  (em coluna e até 240px quando a altura ≥ 820px), chips e botões crescem com a altura.
- Coluna direita sem QR (visão remota, aberta pelo IP da LAN): mesmo ritmo vertical da esquerda (`gap:clamp(12px,2.8vh,32px)`); Agora e Acesso ficam compactos no topo, sem esticar. Medido sem scroll de página e sem overflow nas colunas em 1280×720, 1024×768 e 1440×900, nos modos remoto e local.
- Verificado sem scroll de página (`scrollHeight == innerHeight`) em 1920×1080, 1440×900, 1280×720,
  1024×768 (1366×768 não foi medido diretamente; cai entre 1280×720 e 1440×900 na mesma regra). Abaixo de 600px de altura ou 900px de largura
  vira o layout mobile (scroll em vez de espremer).

### Mobile — largura < 900px ou altura < 600px

Mesma linguagem, coluna única com scroll natural: barra compacta (marca, pill, tema; a nota de
simulação desce para uma 2ª linha), herói no topo (órbitas atrás), depois Agendar, Agora, Acesso e rodapé.
Alvos ≥44px, inputs 16px, `safe-area-inset-*`, `100dvh` com fallback `100vh`. A ordem do DOM é a ordem
visual (herói primeiro) — foco por teclado e leitor de tela seguem a leitura.
A 360px o QR vira coluna; linhas campo+botão usam `flex-wrap` (o botão desce, o rótulo nunca quebra).

### Órbitas

SVG `viewBox 0 0 1000 1000`, `preserveAspectRatio="xMidYMid slice"`, `vector-effect:non-scaling-stroke`
(linhas sempre de 1px). Três grupos (`.g1 .g2 .g3`) com círculos finos, pontilhados e arcos mais
fortes. Ocioso = estáticas. Com agendamento (`body.live`) giram devagar (140s/200s/260s, sentidos
alternados); `prefers-reduced-motion` zera animações e transições (verificado: `animation-name:none`).
O texto sobre as órbitas (rótulo do herói e `#actionLabel`) tem fundo `--bg` para a linha não cortar as letras.

## Modo silencioso: o herói diz quem segura a contagem

O servidor guarda a contagem e só entrega ao Windows no último minuto (`HANDOFF_S = 60`, espelha o servidor). Risco aceito pelo usuário: **se o shutlite não estiver rodando no horário, o PC não desliga**. A UI não esconde isso. O snapshot traz `mode`: `"local"` (contagem só no shutlite) | `"windows"` (entregue) | `null` (ocioso).

| Estado | Rótulo do anel | Nota (`#modeNote`, mono 11px, `aria-live=polite`) | Observações |
| --- | --- | --- | --- |
| `local`, online | `DESLIGA EM` / `REINICIA EM` | "A contagem é do shutlite: o PC só desliga se ele continuar rodando." (`--secondary`) | toast de sucesso inalterado ("Desligamento em 30 min. Dá para cancelar a qualquer momento.") |
| `windows` | idem | "Entregue ao Windows: o aviso dele vai aparecer." | a transição `local` → `windows` por frame do servidor mostra o toast "Entregue ao Windows. O aviso dele vai aparecer." (sem som, 3,2 s); toast de sucesso ganha " O Windows vai avisar." |
| `windows` nascido já em ≤ 60 s ("Desligar agora") | idem | só a nota de Windows, **nunca a de local** | o palpite otimista usa `seconds <= HANDOFF_S` e o servidor confirma |
| `local` + **sem conexão** | `SEM CONFIRMAÇÃO` | "Sem conexão com o shutlite: o horário não está garantido." (`--danger`, 600) | o contador segue girando, mas o anel e o relógio ficam em `--muted` (`.hero.unconfirmed`): não é garantia. Volta ao normal ao reconectar |
| `windows` + sem conexão | normal | nota de Windows | o Windows já tem o timer: contador mantido, só a pill mostra "Sem conexão" |
| ocioso | `PARADO` | oculta | — |

**Descartado**: se o estado passa de agendado para ocioso com mais de 5 s ainda no relógio local, e este aparelho não cancelou/agendou (não há `busy`, `pendingNote` nem ação própria nos últimos 3 s), mostra toast de erro (6 s): "O agendamento foi descartado antes do horário. Veja o log do shutlite." Cobre falha no último minuto, PC suspenso e reinício do servidor que perdeu o estado. Não dispara na contagem chegando a zero (≤ 5 s sobrando) nem no cancelar/agendar deste aparelho. **Limite conhecido**: cancelar em *outro* aparelho é indistinguível e mostra o mesmo toast.

Contraste (medido, ver tabela acima): `--secondary` sobre `--bg` 8,32 / 11,06; `--danger` sobre `--bg` 5,79 / 8,64; `--muted` (anel/relógio sem confirmação) 5,71 / 7,19.

## Component patterns

- **Primário** `.btn-primary`: sólido `--btn`/`--on-btn`, 48px+, mono maiúsculo, hover `opacity:.88` só em `(hover:hover)`, press `scale(.98)`.
  "Desligar agora" usa o primário do sistema, sem destaque especial (decisão do usuário); a confirmação continua obrigatória.
- **Neutro** `.btn-line`: transparente + borda `--border`. **Destrutivo** `.btn-danger`: transparente, texto e borda `--danger`.
- **Seg** `.seg` (Desligar/Reiniciar): trilho com borda, opção ativa sólida (`aria-pressed`), alvo ≥44px.
- **Chips de tempo** `.chip`: um toque agenda (são ações, não seleção); numeral 20px/800 + unidade em mono; slider `accent-color:--accent` espelha o campo e o botão repete o valor.
- **Herói**: `+ 10 min` e `Cancelar` só quando agendado; `aria-live` fica no `#live` (`.sr`, `role="status"`).
- **Cancelar tudo** `.linkbtn`: texto sublinhado, neutro, 44px, sempre visível no card "Agora", inclusive ocioso (o painel ocioso não prova que o Windows está limpo; o servidor pode pedir "Use Cancelar tudo para garantir"). Mesmo fluxo do Cancelar do herói (`cancelAll`: busy, "Cancelando…"); se nada estava agendado o toast diz "Nada estava agendado. O PC continua ligado."
- **PIN**: mínimo 6 caracteres (`PIN_MIN`, espelha o servidor), placeholder "6 dígitos"; pedido num `<dialog>` quando uma ação precisa dele; no PC vem do servidor, mascarado ao lado do QR (Mostrar/Ocultar muda só o texto, sem `aria-pressed`).
- **Diálogo de PIN** (`#pinDlg`): abre ao tocar uma ação sem PIN salvo; foco em `#pinIn` (`type=password`, `inputmode=numeric`, placeholder "6 dígitos"), Esc/Voltar fecham sem salvar e devolvem o foco ao botão de origem. O erro de PIN curto aparece **dentro do diálogo** (`#pinErr`, `role=alert`, `--danger`), não em toast: o toast fica atrás do fundo escuro do modal e some. PIN errado no servidor (401) fecha o diálogo, esquece o PIN e mostra o toast "PIN incorreto…".
- **Acesso remoto** (celular): campo de PIN + `Salvar` na mesma linha; `Salvar` tem largura do conteúdo (`.btn.fit`) e o campo ocupa o resto. QR no PC sempre em fundo branco com margem de 16px (zona de silêncio).
- **QR**: `#qrCode` (`role="img"`) com o QR; o texto de reserva fica fora do `role="img"` (`#qrNote`). Cores lidas de `--qr-ink`/`--qr-bg`. Desenhado a 280px e exibido por CSS com `image-rendering:pixelated`.
- **Toast**: visual `#toast` (`aria-hidden`) invertido (`--toast`) + anúncio em `#live`. Sucesso ~3,2s, erro ~6s e fala a correção. Mensagens de tempo "em 30 min" (segundos abaixo de 1 min); trocar um agendamento ativo cita o que foi substituído.
- **Requisição em andamento**: chips e botões de ação `disabled` até a resposta; Cancelar mostra "Cancelando…" e só vira ocioso após o servidor confirmar.
- **Confirmação destrutiva**: `<dialog>` nativo, borda `--text`, botão repete a consequência ("Desligar"), foco inicial em "Voltar".
- **Status**: `.pill` (mono) + `.dot` quadrado; agendado = azul sólido, ocioso = contorno + dot cinza, sem conexão = `--danger`.
- **Tema**: `#btnTheme` (ícone lua/sol, `aria-label` "Usar tema claro/escuro" que descreve a ação).
- **Foco**: `outline:2px solid var(--accent)` com offset 2px em botões e inputs.
- **Motion**: transições nomeiam propriedade, ≤150ms interativo (anel 300ms); `prefers-reduced-motion` zera tudo.

## Notas

- QR via qrcodejs 1.0.0 (MIT) servido localmente em `/qrcode.min.js`, com fallback em texto. Nada é carregado de fora.
- Tempo real via SSE (`GET /api/events`): servidor faz broadcast em todo schedule/cancel; página pinta na hora.
  Polling de `/api/status` é reserva (1s até o stream abrir ou se cair, 5s de reconcile) + `rev` monotônico:
  a página descarta frame com `rev` menor que o atual, exceto logo após queda ou reconexão (`resync`),
  quando o servidor pode ter reiniciado e o `rev` voltou a 0. Render é local: `deadline` + intervalo de 500ms
  (`renderLocal`); snapshot só re-ancora. Ações pintam otimista (`optimistic`) e o POST confirma; falha faz
  rollback para o estado anterior da tela e `tick()`. Anel usa `stroke-dashoffset` (CIRC=552.9, r=88).
- Relógio com `h:mm:ss` usa classe `.long` (fonte menor) para caber no anel.
- PIN persiste em `localStorage` (`shutlite-pin`); API exige PIN em todo POST.
- Pareamento: o QR do painel aberto no PC aponta para `http://IP:porta/#pin=…`; a página lê o fragmento,
  salva o PIN e limpa a URL. `GET /api/info` só entrega o PIN para loopback.
- Snapshot traz `total`, então o anel continua proporcional depois de recarregar.
- IDs preservados no redesign (o JS depende deles): `dot pill sub hero ring* clock clockCap ringBar actionLabel heroActions
  btnExtend btnCancel btnCancelAll btnNow btnRestart slider mins at btnCustom btnAt qr qrCode qrNote qrurl pinShow btnReveal
  pin btnPin pinSaved dlg* pinDlg* pinForm pinIn pinCancel toast toastMsg live`; novos: `btnTheme`, `ver`.
