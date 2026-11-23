# Mudanças

## 2.2.0

Primeira versão com executável: baixe o `shutlite-2.2.0.exe` e dê dois cliques, sem instalar o Python.

**Novidades**

- `shutlite.exe`: abre o painel no navegador ao iniciar e mostra os endereços numa janela. Fechar a janela para o shutlite.
- `--autostart on` no `.exe` inicia o shutlite minimizado junto com o Windows.
- Modo silencioso: o shutlite conta o tempo sozinho e só chama o Windows no último minuto, sem o aviso de desligamento a cada toque no painel.
- Interface e identidade visual novas, com tema claro e escuro.
- `--check` diz se o servidor está rodando; `--open` abre o painel no navegador.

**Correções**

- Parar o shutlite enquanto um comando ao Windows estava em andamento podia dizer que o desligamento "NÃO vai acontecer" quando ele ia acontecer. Agora o aviso diz que ele pode acontecer e como impedir.
- Reagendar substitui o agendamento anterior em vez de falhar em silêncio.
- O agendamento sobrevive a um reinício do shutlite, e saltos de relógio ou suspensão descartam a contagem em vez de desligar fora de hora.

**Segurança**

- PIN aleatório de 6 dígitos na primeira execução, com trava após erros seguidos.
- Só aceita clientes da rede local e recusa pedidos vindos de outros sites.

**Aviso**

O `.exe` não tem assinatura digital. Na primeira vez o Windows mostra "O Windows protegeu o computador": clique em **Mais informações** e depois em **Executar assim mesmo**. Os SHA-256 dos arquivos estão em `SHA256SUMS.txt`.

## 2.1.0

Primeira versão publicada no GitHub: servidor em Python puro e painel web para desligar ou reiniciar o PC pela rede local.
