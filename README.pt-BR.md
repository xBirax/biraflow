<p align="center"><img src="biraflow/assets/logo.png" alt="bira flow" width="420"></p>

# Bira Flow para Windows

Ditado por voz em qualquer aplicativo do Windows 11, no estilo do Wispr Flow. Segure uma
tecla, fale e solte: o texto é transcrito, limpo e colado onde está o cursor.

> Antes se chamava **Whisperflow**. Na primeira execução, as pastas antigas
> (`%APPDATA%\Whisperflow` e `%LOCALAPPDATA%\Whisperflow`) são movidas para `BiraFlow`, sem
> baixar o modelo de novo, e o "Iniciar com o Windows" é mantido. Se as duas versões existirem,
> a página **Sobre** mostra o espaço das pastas antigas e oferece apagá-las.

- **Segurar para falar**: grava enquanto o atalho está pressionado, transcreve e cola ao soltar.
- **Toque duplo = mãos livres**: grava sem segurar nada; um novo toque encerra. Para sozinho
  em 30 minutos (ajustável até 1 hora), para uma sessão esquecida não gravar a tarde inteira.
  Ditados longos são transcritos em partes de 10 min, cortadas numa pausa.
- **Reuniões**: grava o seu microfone e o que o PC toca (Teams, Zoom, Meet…), sem bot na chamada.
  A transcrição sai aos poucos, com "Eu" e "Participantes" e a minutagem, e no fim vêm as notas:
  resumo, decisões e próximos passos.
- **Ditado em sequência**: dá para começar o próximo ditado enquanto o anterior ainda está
  sendo transcrito; os textos entram na ordem em que foram falados.
- **Esc cancela**: descarta a gravação ou interrompe uma transcrição em andamento. O Esc é só
  observado, então continua chegando ao app em que você está digitando.
- **Transcrição pela nuvem**: com uma chave no `.env`, o padrão é a API Whisper da **Groq** (ou da
  **OpenAI**); sem chave, o **modelo local** (whisper.cpp, offline). Dá para fixar qualquer um dos
  dois na configuração.
- **Limpeza por LLM**: remove vícios de fala ("é…", "tipo", "né", "um", "uh"), corrige pontuação
  e maiúsculas, sem traduzir nem reescrever.
- **Inserção no cursor**: área de transferência + Ctrl+V (ou digitação simulada). O ditado fica
  na área de transferência para colar de novo; uma opção devolve o que estava lá antes.
- **Pílula flutuante** enquanto grava, mostrando o modo (Ditando / Mãos livres / Comando), o
  tempo e o nível do microfone. Ela nunca rouba o foco nem bloqueia cliques.
- **Dicionário com correções** ("ese" → "esse", "cloud code" → "Claude Code") que **aprende**
  quando você corrige um ditado no histórico.
- **Snippets**: diga "meu email" e entra o seu endereço; diga "assinatura" e entra a assinatura.
- **Estilos por aplicativo**: casual no WhatsApp, técnico no VS Code e, no Outlook/Gmail, **formato de
  e-mail**: a saudação numa linha, parágrafos curtos com uma linha em branco entre eles, a despedida e o
  "Att," com o nome em linhas próprias — só com o que você falou (a assinatura fica por conta do e-mail).
- **Modo comando** (**Ctrl+Win+Alt**): selecione um texto e diga "deixe mais formal" ou "traduza
  para o inglês"; sem seleção, ele escreve o que você pedir.
- **Colar último ditado** (**Shift+Alt+Z**), **histórico** pesquisável com texto bruto e
  estatísticas (palavras por minuto, tempo economizado).
- **"Pressione enter"** no fim do ditado envia a mensagem; **pontuação falada** ("vírgula",
  "nova linha") e **limpeza offline** de hesitações, mesmo sem LLM.
- **Sons** de início, fim e cancelamento, **silenciar música** durante o ditado e **ganho
  automático** para quem fala baixo.
- **Janela de configurações** no visual do Windows 11 (barra lateral, ícones Fluent, modo
  claro/escuro), com gravação de atalhos e teste do microfone.
- **Ícone na bandeja** com liga/desliga e iniciar com o Windows; um clique abre as configurações.
- **Sem contas, sem telemetria**. Sem chave nenhuma, funciona 100% offline com o modelo local.
  Com chave, o áudio do ditado vai para a API que você escolher (Groq ou OpenAI).

## Instalação

Requisitos: Windows 10/11 64 bits e [Python 3.11+](https://www.python.org/downloads/)
(no instalador, marque "Add python.exe to PATH").

```powershell
# 1. Ambiente virtual, dependências, arquivo de chaves e atalho no Menu Iniciar
powershell -ExecutionPolicy Bypass -File scripts\install.ps1

# 2. Modelo local (OPCIONAL, ~470 MB em %LOCALAPPDATA%\BiraFlow): sem ele o ditado usa a API
#    (basta a chave da Groq no .env); com ele, funciona também offline.
#    (também pelo próprio app: Configurações → Transcrição → Instalação local)
powershell -ExecutionPolicy Bypass -File scripts\setup-whisper.ps1
#    mais preciso (mais lento):       -Model medium
#    CPU moderna, mais rápido:        -Build blas
#    placa NVIDIA:                    -Build cuda12 -Model large-v3-turbo

# 3. Conferir o que foi detectado
.venv\Scripts\python -m biraflow --check
```

Para iniciar, procure **Bira Flow** no Menu Iniciar (ou rode `.venv\Scripts\biraflow.exe`).
Para ver o log no terminal, use `.venv\Scripts\python -m biraflow`. Para iniciar junto com o
Windows, marque **Iniciar com o Windows** no menu da bandeja.

**Para uma equipe, tudo em nuvem** (sem modelo local, em computadores fracos): veja
`docs/EQUIPE-NUVEM.md` e use `scripts\setup-team.ps1`, que instala em silêncio, grava as chaves e
deixa `engine = "auto"` com a limpeza no Vercel AI Gateway.

## Instalador do Windows (assinado) e atualizações automáticas

Para usar o Bira Flow sem Python, gere o instalador:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\build-installer.ps1 -Repo usuario/bira-flow
```

O script instala o PyInstaller e o Inno Setup (ambos gratuitos), cria na primeira vez o
certificado de assinatura e gera `dist\BiraFlow-Setup-<versão>.exe` (~24 MB) e `dist\latest.json`.
O instalador não pede administrador: instala em `%LOCALAPPDATA%\Programs\Bira Flow`, cria o
atalho no Menu Iniciar e, se você marcar, inicia com o Windows. Configurações, chaves e modelos
continuam em `%APPDATA%\BiraFlow` e `%LOCALAPPDATA%\BiraFlow` e são mantidos ao desinstalar. A
instalação inclui `biraflow-cli.exe --check` e `--selftest` para diagnóstico.

**Assinatura sem custo.** Os executáveis e o instalador são assinados com um certificado próprio
(autoassinado, SHA-256, com carimbo de tempo gratuito). Ele garante que o arquivo não foi
alterado e que veio de você. Para o Windows mostrar "Editor: Bira Flow" como válido, cada PC
precisa confiar no certificado uma vez (Win+R):

```powershell
powershell -ExecutionPolicy Bypass -File F:\Whisperflow\scripts\trust-signing-cert.ps1
```

Em outros PCs, sem confiar no certificado, o Windows SmartScreen avisa "editor desconhecido" ao
baixar o instalador pela internet. Assinatura confiável em qualquer PC só existe de graça em dois
casos: projeto open source aprovado pela SignPath Foundation, ou publicação na Microsoft Store
(que assina o pacote). Fora disso, um certificado de código custa a partir de ~US$ 100/ano.

A chave privada fica em `packaging\signing\` (fora do git). **Faça backup**: as atualizações
automáticas só aceitam instaladores assinados com esse mesmo certificado.

**Atualizações automáticas.** A versão instalada verifica um manifesto (`latest.json`) nos
Releases do GitHub ao abrir e a cada 6 horas. Havendo versão nova, ela baixa o instalador,
confere o SHA-256 e a assinatura (precisa ser do certificado fixado no build; um arquivo
adulterado é recusado), espera você terminar de ditar, instala em silêncio e reabre o app. Para
lançar uma versão: aumente `__version__` em `biraflow\__init__.py`, rode `build-installer.ps1` e
depois:

```powershell
powershell -ExecutionPolicy Bypass -File scripts\publish-release.ps1 -Notes "O que mudou"
```

Dá para desligar ou verificar na hora em Configurações → Sobre → Atualizações.

## Uso

| Ação | O que acontece |
|---|---|
| Segurar o atalho (padrão **Ctrl+Win**) | Grava enquanto estiver pressionado; ao soltar, transcreve e cola |
| Tocar duas vezes rápido | Modo mãos livres: grava sem segurar nada |
| Tocar uma vez (no mãos livres) | Encerra, transcreve e cola |
| **Esc** gravando | Descarta a gravação |
| **Esc** com "Transcrevendo…" na tela | Cancela: nada é colado |
| Um toque curto sozinho | Ignorado (nada é gravado) |
| Atalho + outra tecla (ex.: Ctrl+Win+D) | Tratado como atalho normal do Windows; a gravação é descartada |
| Segurar **Ctrl+Win+Alt** com um texto selecionado | Modo comando: a instrução falada reescreve a seleção |
| **Shift+Alt+Z** | Cola de novo o último ditado |
| Terminar o ditado com "pressione enter" | Cola e aperta Enter (envia a mensagem) |
| Terminar o ditado com "cancelar ditado" | Descarta o ditado (como o Esc) |
| Ditar só "apagar último ditado" | Apaga o texto que acabou de ser colado (se você não digitou nada depois, na mesma janela, em até 2 min) |
| Mãos livres esquecido ligado | Para e transcreve sozinho depois de 60 s de silêncio (Microfone → Limites) |
| Bloquear o PC (Win+L) gravando | A gravação é descartada |

A pílula mostra a sua voz como uma onda que sobe e desce enquanto você fala. Em Configurações →
Geral dá para escolher a posição (em cima ou embaixo, à esquerda, no centro ou à direita), o
tamanho (pequena, média, grande) e se ela mostra o texto inserido logo depois de colar.

Cores da pílula: vermelho = segurando, roxo = mãos livres, turquesa = comando,
âmbar = transcrevendo. Nos últimos 30 segundos do mãos livres ela avisa "para em Ns". Se o
microfone entregar silêncio absoluto, ela avisa "Microfone sem som (mudo?)"; se o Windows estiver
bloqueando o microfone para apps (Privacidade → Microfone), a mensagem diz como liberar.

**Microfone pronto** (Configurações → Microfone, desligado por padrão): mantém o microfone aberto
por 30 s depois de cada ditado. O próximo começa na hora e ainda guarda os 0,3 s antes do atalho,
então a primeira sílaba nunca é cortada. Nesse tempo o ícone de microfone do Windows fica aceso;
nada é gravado nem enviado fora de um ditado.

## Recursos inspirados no Wispr Flow

Comparação com os recursos documentados do Wispr Flow (out/2026):

| Wispr Flow | Bira Flow | Observação |
|---|---|---|
| Push-to-talk Ctrl+Win, mãos livres, Esc | ✓ | Mãos livres por toque duplo |
| Dicionário: palavras | ✓ Palavras (vocabulário) | Enviadas como dica ao Whisper e ao LLM |
| Dicionário: regras de correção | ✓ Correções | Palavra inteira, sem diferenciar maiúsculas; a grafia salva é mantida |
| Aprender com correções | ✓ Corrigindo onde colou (WhatsApp, navegador, Word…) ou no Histórico → Corrigir… | |
| Snippets | ✓ | Funciona com "e-mail" ou "email", no meio da frase ou sozinho |
| Estilos por app | ✓ 5 categorias, 5 tons | Também funciona para português, não só inglês |
| Modo comando (Ctrl+Win+Alt) | ✓ | Precisa de um provedor de LLM; em terminais não copia a seleção (Ctrl+C interromperia o programa) |
| Colar último ditado (Shift+Alt+Z) | ✓ | Também no menu da bandeja |
| Histórico, ver bruto × final, estatísticas | ✓ | Retenção: sempre, 24 h ou nunca |
| Silenciar música durante o ditado | ✓ | Silencia só os apps que estão tocando e restaura exatamente esses |
| Som de início | ✓ + fim e cancelamento | |
| Enviar (Return) | ✓ "pressione enter" | |
| Funciona offline | ✓ com o modelo local | O padrão é a API (Groq/OpenAI) quando há chave; sem chave, ou fixando `engine = "local"`, roda 100% offline |
| Sem conta e sem telemetria | ✓ Diferencial | |
| Sincronização entre dispositivos, equipes, notas, Scratchpad | — | Fora do escopo de um app local |

## Atalho

Clique no ícone da bandeja → aba **Atalho** → **Gravar novo atalho** e pressione a combinação.
Também dá para editar `keys` em `settings.toml` (botão **Abrir arquivo avançado…**); o arquivo
é recarregado sozinho ao salvar.

```toml
[hotkey]
keys = "ctrl+win"      # também: "rctrl", "ralt", "f9", "capslock", "ctrl+alt+space", "vkE9"
```

- **A tecla Fn não serve.** Na maioria dos notebooks ela é tratada pelo próprio teclado e nunca
  chega ao Windows, então nenhum programa consegue lê-la. Boas alternativas: **Ctrl+Win**
  (igual ao Wispr Flow), **Ctrl direito** (`rctrl`, fácil de tocar duas vezes), **Alt direito**
  (`ralt`), **CapsLock** ou **F9**. Alguns notebooks (Lenovo, por exemplo) permitem trocar Fn e
  Ctrl na BIOS ou no utilitário do fabricante.
- Com `suppress_keys = true`, teclas comuns do atalho (CapsLock, F9, Espaço…) não chegam ao app:
  o CapsLock não alterna, por exemplo. Modificadores (Ctrl, Shift, Alt, Win) e o Esc nunca são
  bloqueados.
- Atalhos com Win ou Alt não abrem o Menu Iniciar nem a barra de menus ao soltar: o Bira Flow
  envia uma tecla "máscara" inofensiva (vkE8), a mesma técnica do AutoHotkey.
- Teclas remapeadas pelo AutoHotkey ou por outro programa também funcionam como atalho.

## Ativação por voz ("Ei Bira")

Diga a frase de ativação e o ditado começa sem tocar no teclado, como na Alexa. Configure em
**Configurações → Ativação por voz**: ligar/desligar, a frase (padrão "Ei Bira", troque pelo que
quiser), quantos segundos de silêncio encerram o ditado e se a frase é confirmada pelo Whisper.
Na primeira vez, clique em **Baixar modelo de voz** (32 MB, uma vez só).

Como funciona:

1. **Detector local, sempre ligado.** Um reconhecedor offline (Vosk, modelo pequeno em português)
   escuta o microfone e procura só a frase, gastando ~2–3% de um núcleo do processador. O modelo
   pequeno não conhece nomes como "Bira", então a frase é escrita com palavras que ele conhece e
   soam igual ("bi ra", "vira", "beira").
2. **Confirmação pelo Whisper.** Quando o detector ouve algo parecido, só esse trecho (~2,5 s) é
   conferido pelo Whisper com a frase como dica: "Ei Bira" passa, "ei, vira aqui" não. Com o modelo
   local instalado, a conferência roda no seu PC (~0,2 s na placa de vídeo) e o áudio do ambiente
   nunca sai dele; sem o modelo local, vai para a API de transcrição.
3. **O ditado.** Ao confirmar, a pílula aparece em modo mãos livres, com o áudio desde a frase:
   nada do que você disse logo depois se perde, mesmo sem pausa. A frase sai do texto ("Ei Bira,
   manda o arquivo" vira "Manda o arquivo"). O ditado termina após o silêncio configurado
   (1,5 s), com um toque no atalho ou com Esc (cancela). "Pressione enter" no fim continua enviando.

Enquanto a ativação está ligada, o ícone de microfone do Windows fica aceso. Ela pausa quando o
ditado é desativado (ou pausado por 1 hora pela bandeja) e não reage enquanto você já está ditando.
Prefira duas palavras ("Ei Bira", "Ok Bira", "Oi Flow"): uma palavra só, ou palavras comuns na
conversa, disparam por engano. A página mostra ao vivo o estado, o nível do microfone e o que o
detector ouviu por último.

## Transcrever áudios (WhatsApp)

Abra **bandeja → Transcrever áudio…** e arraste um áudio para a janela: o texto aparece em
segundos e já fica copiado para colar com Ctrl+V (desligue "Copiar ao terminar" se preferir).

- **Do WhatsApp**: no áudio, ⋮ → **Baixar** (WhatsApp Web/Desktop salva um .ogg/.opus em Downloads);
  depois arraste o arquivo para a janela, ou clique em **Último áudio baixado**, que pega o áudio
  mais recente da pasta Downloads.
- **Pelo Explorador**: clique com o botão direito no áudio → **Enviar para → Bira Flow (transcrever
  áudio)**, arraste o arquivo sobre o atalho do Bira Flow, ou copie o arquivo e aperte Ctrl+V na janela.
  Vários arquivos de uma vez entram numa fila.
- **Formatos**: .ogg/.opus (WhatsApp), .mp3, .m4a, .wav, .flac, .webm, .mp4.
- **Motor**: o mesmo da transcrição. Com o Groq, o arquivo sobe como está (um áudio de 24 s leva
  ~1 s); gravações longas são divididas em partes de 10 min, cortadas numa pausa. Sem internet, o
  modelo local transcreve (.ogg/.opus, .mp3, .wav e .flac; .m4a só pela API).
- **Resumir**: um resumo em tópicos (assunto, datas, valores, pedidos e "Ação:" quando pedem algo),
  feito pelo provedor de refinamento. Útil para áudios longos.
- As correções do seu Dicionário valem aqui também ("Kate" → "Kayth"), e cada áudio transcrito
  entra no Histórico como "Áudio (arquivo)".

## Reuniões

**Bandeja → Iniciar reunião** grava o seu microfone e o que o PC toca (o outro lado da chamada),
sem colocar nenhum bot na reunião. Para terminar, **bandeja → Encerrar reunião**. Em poucos
segundos abre uma janela com as notas e a transcrição, prontas para copiar ou salvar.

- **Na primeira reunião** o Bira Flow pede que você confirme que vai avisar os participantes.
  Gravar alguém sem consentimento pode violar a LGPD e as regras da sua empresa.
- **Durante a reunião**: a dica do ícone da bandeja mostra o tempo de gravação. O ditado continua
  funcionando (ele tem prioridade na transcrição), e o "silenciar música" não silencia o PC.
- **Transcrição aos poucos**: a cada ~90 s um trecho é transcrito, então ao encerrar só falta o
  último. Quem fala é rotulado pelo canal: **Eu** (seu microfone) e **Participantes** (o áudio
  do PC). Com **headset** a separação é quase perfeita; com caixas de som, a sua voz também vaza
  para o microfone dos outros e o rótulo pode errar mais.
- **Notas**: no fim, o provedor de Refinamento escreve o título, o resumo, as decisões (com a
  minutagem), os próximos passos ("responsável — tarefa — prazo") e os pontos em aberto, sem
  inventar nomes nem datas. Sem provedor de LLM, fica só a transcrição. O botão **Gerar notas**
  da janela refaz.
- **Pasta**: cada reunião fica em `Documentos\Bira Flow\Reuniões\AAAA-MM-DD_HHMM`, com
  `transcricao.md`, `notas.md` e o áudio (`audio\parte-NNN.ogg`, Opus estéreo, ~21 MB/h).
  **Bandeja → Abrir reuniões…** abre a pasta.
- **À prova de falhas**: se o app fechar, o PC travar ou você sair no meio, a próxima abertura
  termina a transcrição e as notas sozinha. Se o PC suspender, a pausa fica marcada na
  transcrição. Um trecho que falhou (sem internet, por exemplo) é tentado de novo; se continuar
  falhando, o botão **Tentar de novo** aparece na janela da reunião.
- **Custo**: com o Groq, cerca de US$ 0,04 por hora de reunião, e uma hora gasta metade do limite
  de áudio por hora do plano gratuito. O modelo local (placa de vídeo) é grátis e não envia o
  áudio. As notas são uma ou poucas chamadas ao LLM.
- **Configurações → Reuniões**: gravar ou não o áudio do PC, guardar ou apagar o áudio depois de
  transcrito, gerar as notas ao encerrar e encerrar sozinho depois de tantas horas (padrão 4,
  de 1 a 8).

## Transcrição

```toml
[transcription]
engine = "auto"         # padrão: API se houver chave (Groq, OpenAI ou Vercel Gateway), senão local
                        # "local" | "groq" | "openai" | "gateway" fixam um motor
gateway_model = "spacexai/grok-stt"   # motor `gateway`: modelo de transcrição do AI Gateway
language = "auto"       # ou "pt", "en"…
local_model = "small"   # "small" | "medium" | "large-v3-turbo" | caminho para um .bin
vocabulary = ["Kubernetes", "Maria"]   # nomes que o reconhecimento deve acertar
```

| Motor | Privacidade | Tempo medido (9 s de fala) | Observação |
|---|---|---|---|
| `local` **large-v3-turbo** + placa NVIDIA | O áudio nunca sai do PC | **~0,30 s** (RTX 4050) | Melhor qualidade; o mesmo modelo que a Groq usa |
| `local` small + placa NVIDIA | O áudio nunca sai do PC | ~0,25 s (RTX 4050) | Erra mais nomes e termos técnicos |
| `local` small, `language = "pt"` | O áudio nunca sai do PC | ~1,5 s (i7-13700H, CPU) | Texto idêntico ao original no teste |
| `local` small, `language = "auto"` | O áudio nunca sai do PC | ~3,4 s | A detecção de idioma custa ~2 s |
| `local` medium | O áudio nunca sai do PC | ~3× o small | Mais preciso, ~1,5 GB |
| `groq` | O áudio vai para a Groq | **~0,44 s** medido do Brasil (conexão reaproveitada) | `whisper-large-v3-turbo`, mais estável que a GPU de notebook |
| `openai` | O áudio vai para a OpenAI | ~1–2 s (estimado) | `whisper-1` |

**Placa de vídeo NVIDIA:** em Configurações → Transcrição → Instalação local, escolha o pacote
"Placa NVIDIA (CUDA 12)" (~700 MB) e clique em **Instalar whisper.cpp**. Com "Manter o modelo
carregado na placa de vídeo" ligado (padrão), o modelo fica na GPU enquanto o app está aberto
(~500 MB de memória de vídeo) e cada ditado leva ~0,25 s. A primeira inicialização depois de
instalar pode levar ~15 s (o driver prepara a placa uma única vez); enquanto isso, os ditados usam
o processador normalmente.

**Dica de velocidade:** fixe o idioma (`language = "pt"`) se você dita sempre na mesma língua.
Com `local_fast = true` (padrão), o whisper.cpp usa decodificação gulosa e uma janela de
codificação do tamanho da fala em vez dos 30 s fixos do Whisper: ~2,5× mais rápido, com o mesmo
texto em gravações de ditado. Acima de 28 s, a janela completa volta a ser usada.

Os tempos são estimativas e variam com o hardware. Com `fallback_to_local = true`, se a API
falhar (por exemplo, sem internet), o Bira Flow tenta o modelo local.

**Dicas para o Whisper**: além do `vocabulary`, as grafias que você ensinou ao Dicionário
("GitHub", "Kayth") vão como dica para a transcrição, então passam a ser ouvidas certo, e não só
corrigidas depois. A última frase ditada no mesmo app (há menos de 2 minutos) também vai como
contexto. Quando a nuvem transcreve e a GPU é só a reserva, o modelo local sai da memória de
vídeo depois de 10 minutos sem uso (volta sozinho quando for preciso).

## Limpeza por LLM

O texto bruto passa por um prompt curto que remove vícios de fala, corrige pontuação e
maiúsculas, transforma "novo parágrafo" em quebra de linha e nunca responde ao que foi ditado.

```toml
[cleanup]
provider = "auto"    # primeira chave do .env: Groq → OpenAI → Anthropic → Vercel; "ollama" = 100% local; "none" desliga
model = ""           # vazio = padrão do provedor (Groq: qwen/qwen3.8-27b, OpenAI: gpt-4.1-mini, Anthropic: claude-haiku-5-5, Vercel: google/gemini-2.5-flash-lite)
extra_instructions = "Use português do Brasil."
```

**Limite do Groq (erro 429):** o plano gratuito limita pedidos por minuto *por modelo*. Quando o
modelo padrão bate no limite, o Bira Flow usa na hora `openai/gpt-oss-20b` (e depois
`gpt-oss-120b`), e deixa o modelo limitado descansar pelo tempo que o Groq pedir. Só quando todos
estão no limite o texto sai sem refinamento, com aviso na pílula. Isso vale para o modelo padrão;
se você escolheu um modelo, ele é respeitado.

Se o LLM falhar, demorar mais que `timeout_seconds` ou responder algo que não parece o ditado
limpo, o texto bruto é colado. Você nunca perde o que falou. Sem chave nenhuma, a limpeza é
desligada e nada sai do computador.

As chaves são digitadas na aba **Chaves de API** e ficam em `%APPDATA%\BiraFlow\.env`:

```ini
GROQ_API_KEY=gsk_...
OPENAI_API_KEY=sk-...
ANTHROPIC_API_KEY=sk-ant-...
AI_GATEWAY_API_KEY=vck_...
```

**Vercel AI Gateway:** uma chave só para modelos de vários laboratórios, pelo preço de tabela de
cada um (sem taxa da Vercel). O padrão é `google/gemini-2.5-flash-lite` (0,65 s por frase,
medido), que também lê os prints. Para notas de reunião mais caprichadas, use
`google/gemini-3.8-flash` ou `anthropic/claude-sonnet-5.5` em `model`. Se o seu time da Vercel usa
a *Model Allowlist*, o modelo escolhido precisa estar liberado lá; senão o app avisa e cola o texto
sem refinamento. O Gateway serve só o LLM: a transcrição continua no
Groq, na OpenAI ou no modelo local.

## Inserção e área de transferência

```toml
[output]
method = "paste"            # "paste" (Ctrl+V) ou "type" (digita caractere por caractere)
restore_clipboard = false   # true = devolve o conteúdo anterior da área de transferência
paste_keys = "ctrl+v"       # alguns terminais preferem "shift+insert"
trailing_space = true
smart_insert = true         # encaixa no texto que já existe (veja abaixo)
```

- **Encaixar no texto que já existe** (`smart_insert`): antes de colar, o Bira Flow lê os 3
  caracteres antes do cursor. No meio de uma frase ("Vou levar o |") o ditado começa com
  minúscula e ganha o espaço que faltar; nomes próprios (que o corretor do Windows só aceita com
  maiúscula, ou que estão no seu vocabulário/dicionário) continuam com maiúscula. Se o app não
  informa o texto, nada muda.
- **App aberto como administrador**: o Windows bloqueia teclas simuladas nesse caso. Em vez de
  "colar no vazio", o Bira Flow avisa e deixa o texto na área de transferência (Ctrl+V).
- Com `restore_clipboard = false` (padrão), o ditado fica na área de transferência para colar
  de novo. Também dá para usar **Copiar último ditado** na bandeja.
- Com `restore_clipboard = true`, todo o conteúdo anterior (texto, imagens, formatos ricos) é
  devolvido logo depois da colagem, e o ditado temporário não entra no histórico do Win+V.
  Se você copiar algo nesse meio-tempo, o Bira Flow não sobrescreve.
- O ditado nunca é enviado para a área de transferência na nuvem do Windows.

## Comandos de voz e atalhos de texto

| Diga | Sai |
|---|---|
| "joão arroba gmail ponto com" | joao@gmail.com |
| "reticências", "travessão", "abre/fecha colchetes" | `...`, ` — `, `[ ]` |
| "emoji coração", "emoji de joinha", "emoji risada"… | ❤️ 👍 😂 (lista em `textproc.EMOJIS`) |
| "… cancelar ditado" (no fim) | nada: o ditado é descartado, como o Esc |
| "… pressione enter" (no fim) | cola e aperta Enter (enviar) |

Snippets aceitam campos que se preenchem na hora: `{data}` (15/10/2026), `{data_extenso}`
(15 de outubro de 2026), `{hora}`, `{dia_semana}` e `{amanha}`. Ex.: gatilho "assinatura",
texto `Bira — {data_extenso}`.

Ditados de até 2 palavras ("ok", "pode ser") não passam pelo LLM: saem na hora, só com as regras
locais.

## Permissões no Windows

No macOS seria preciso liberar **Acessibilidade** e **Monitoramento de Entrada**. **O Windows
não tem essas permissões**: o hook de teclado global (`WH_KEYBOARD_LL`) e a simulação de teclas
(`SendInput`) funcionam sem autorização especial. O que precisa ser verificado:

1. **Microfone** (obrigatório). Abra *Configurações → Privacidade e segurança → Microfone* e ligue:
   - **Acesso ao microfone**
   - **Permitir que os aplicativos da área de trabalho acessem o microfone**

   O Bira Flow é um app de área de trabalho (Python), então não aparece na lista de apps da
   Store; vale a chave geral acima. Enquanto ele grava, o Windows mostra o ícone de microfone na
   bandeja. Fora da gravação o microfone fica fechado.
2. **Apps rodando como administrador.** O Windows isola janelas elevadas (UIPI): enquanto um app
   aberto "como administrador" estiver em foco, o atalho não é detectado e a colagem é bloqueada.
   Se precisar ditar nesses apps, inicie o Bira Flow também como administrador. Normalmente
   não é necessário.
3. **Telas protegidas.** Tela de bloqueio, prompt do UAC e Ctrl+Alt+Del não aceitam ditado, por
   design do Windows.
4. **Antivírus.** Alguns antivírus estranham programas com hook de teclado. Se o seu reclamar,
   libere a pasta `.venv` do projeto. O código é todo local e está nesta pasta.
5. **Firewall.** Nenhuma porta é aberta. As únicas conexões de saída são para as APIs que você
   configurar.

## Dicionário, snippets e histórico

- **Correções** (página Dicionário): "Quando ouvir" → "Escrever". Aplicadas depois do LLM, então
  ele não consegue desfazê-las. No início de frase, uma correção em minúscula ganha maiúscula.
- **Aprender com as suas edições** (como o Wispr Flow): se logo depois de ditar você corrigir uma
  palavra no próprio app ("ese" → "esse"), a troca vira uma correção sozinha e a pílula avisa
  "Aprendido: ese → esse". O Bira Flow lê, via UI Automation do Windows, só o campo onde colou o
  texto, por até 5 minutos (se você trocar de janela, ele pausa e retoma quando você volta), nunca campos de senha, e guarda
  só a regra. Funciona em apps que expõem o texto: caixas de texto do Windows, Word e os apps
  feitos em Chromium (Chrome, Edge, Brave, WhatsApp Web e WhatsApp Desktop, Teams, Slack…).
  Nos Chromium, a página só mostra o texto ao Windows quando um leitor de tela pede; por isso, ao
  começar a gravar numa janela dessas, o Bira Flow faz esse pedido (como o Narrador faz), e o
  navegador passa a expor o campo. Corrija **antes** de enviar a mensagem: depois do envio o campo
  esvazia, e o Bira Flow aprende com a última versão que viu. Desligável em Dicionário.
- **Aprender pelo Histórico**: no Histórico, selecione um ditado → **Corrigir…** → edite → **Salvar correção**.
  As trocas de grafia de até 3 palavras viram correções (ex.: "ese" → "esse", "github" → "GitHub").
  Mudanças de conteúdo ("não" → "sim") e números não viram regras.
- **Aprender com um print**: tire um print onde aparece a correção (Win+Shift+S). Pode ser o texto
  que você editou, uma mensagem seguinte como "é Taspio" ou o nome certo em outro lugar da tela.
  Depois clique em **Aprender com o print copiado** no menu da bandeja, ou em **Aprender com um
  print** (ou Ctrl+V) na página Dicionário. Um modelo com visão do seu provedor de refinamento
  (Groq, OpenAI ou Anthropic) lê o print junto com seus últimos ditados e devolve as trocas
  (ex.: "TASP" → "Taspio", "Kate" → "Kayth"), que entram no Dicionário na hora. A imagem só é
  enviada quando você pede. Se algo errado entrar, use **Desfazer aprendizado** na bandeja.
- **Snippets**: o gatilho pode aparecer no meio da frase ("me manda no meu email") ou sozinho.
  O LLM recebe um marcador no lugar do snippet, então o texto salvo entra exatamente como está.
- **Histórico**: `%APPDATA%\BiraFlow\history.json` (só local). As estatísticas guardam apenas
  contadores (palavras e segundos), nunca texto. A busca procura no texto e no nome do app;
  duplo clique corrige; **Exportar…** salva tudo em `.txt` ou `.csv` (planilha).
- **Cópia de segurança do dicionário**: Dicionário → **Exportar…/Importar…** leva palavras,
  correções e snippets (JSON) para outro computador; importar mescla com o que já existe.
- **Snippets com campos**: `{data}`, `{data_extenso}`, `{hora}`, `{dia_semana}`, `{amanha}` e
  `{area_de_transferencia}` (o texto que você tinha copiado).

## Privacidade

- Nenhuma conta, nenhuma telemetria. A versão instalada só consulta o canal de atualizações
  (desligável em Configurações → Sobre).
- Com `engine = "local"` e sem chaves no `.env`, nada sai do computador.
- O áudio é gravado num WAV temporário em `%LOCALAPPDATA%\BiraFlow\tmp`, apagado logo após a
  transcrição (e na próxima inicialização, se o programa tiver fechado no meio).
- O log (`%APPDATA%\BiraFlow\biraflow.log`) registra tempos e erros, nunca o texto ditado.
- O histórico pode ser desligado (Histórico → Guardar ditados → Não guardar) ou limitado a 24 h.
- Os estilos só leem o nome do programa e o título da janela em foco, no momento do ditado.
- O teclado é observado só para reconhecer o atalho e o Esc. Nenhuma tecla é gravada.
- **Reuniões ficam em texto** (e, se você quiser, em áudio) na pasta Documentos, fora do
  `%APPDATA%`: qualquer programa ou backup que leia os seus Documentos (OneDrive, por exemplo)
  vê as transcrições. Apague a pasta da reunião quando não precisar mais dela.

## Bandeja do sistema

O equivalente à barra de menus do macOS: o ícone fica perto do relógio (pode estar escondido em
"^"). A cor indica o estado: azul = pronto, vermelho = gravando, âmbar = transcrevendo,
cinza = desativado. Um clique abre as configurações; o botão direito abre o menu:
Configurações, **Histórico…** e **Dicionário…** (abrem direto na página), ativar/desativar,
**Pausar por 1 hora**, iniciar com o Windows, colar/copiar último ditado, **Transcrever de novo o
último áudio** (quando uma transcrição falhou), **Transcrever áudio…**, **Iniciar reunião** /
**Encerrar reunião**, **Abrir reuniões…**, **desfazer o último aprendizado**, sair. Passando o
mouse, a dica mostra quantos ditados e palavras você fez hoje e, durante uma reunião, o tempo
de gravação.

Na janela de configurações: o rodapé avisa "Alterações não salvas" e pergunta antes de descartar;
**Ctrl+S** salva e **Esc** fecha; a janela lembra o tamanho e a posição. Em Chaves de API,
**Testar chaves** confirma com cada provedor se a chave funciona (só lista os modelos, sem custo).
Em Sobre, **Restaurar padrões** volta as configurações ao padrão sem mexer em chaves, dicionário e
histórico.

## Solução de problemas

| Sintoma | O que verificar |
|---|---|
| Nada acontece ao segurar o atalho | Ícone cinza? Ative na bandeja. Rode `--check` para ver se o atalho é válido. App em foco rodando como administrador? |
| "Microfone indisponível" | Permissões do microfone (acima); `input_device` em `settings.toml` |
| "whisper-cli.exe não encontrado" | Rode `scripts\setup-whisper.ps1` |
| "Nenhuma fala detectada" | Microfone mudo ou muito baixo; diminua `silence_threshold` |
| Pílula diz "Microfone sem som (mudo?)" | O microfone entrega silêncio absoluto: está mudo no Windows, numa tecla de privacidade do notebook ou é o dispositivo errado (`input_device`) |
| Plugou um fone e ele não foi usado | O Bira Flow relê a lista de microfones depois de cada ditado; o próximo já usa o novo padrão do Windows |
| Ícone fixado na barra de tarefas borrado ou antigo | Reinstale (o instalador renova o cache de ícones) ou desafixe e fixe de novo |
| "Limite de uso do Groq" | Plano gratuito do Groq; o app já troca de modelo sozinho (o refinamento e também a transcrição, que passa para o `whisper-large-v3`). Espere alguns segundos |
| Sem internet | O primeiro ditado espera a conexão falhar; os seguintes, por 1 minuto, já vão direto para o modelo local |
| A transcrição falhou e eu perdi o que falei | Não perdeu: o áudio fica guardado. Bandeja → **Transcrever de novo o último áudio** |
| O atalho atrapalha um jogo | Configurações → Atalhos → **Apps ignorados** (ex.: `valorant.exe`), ou bandeja → **Pausar por 1 hora** |
| "App aberto como administrador" | O Windows não deixa um app comum colar num app de administrador. Cole com Ctrl+V |
| Corrigi uma palavra e ela não foi aprendida | Troca entre duas palavras comuns ("casa" → "caso", "esta" → "está") é mudança de conteúdo, não erro de transcrição: adicione no Dicionário se quiser a regra. Nome, marca ou sigla ("cloud" → "Claude", "DR" → "ADR") é aprendido, desde que soe parecido |
| settings.toml quebrado depois de editar à mão | O Bira Flow usa a última versão boa (`settings.toml.bak`) e avisa até você corrigir |
| Texto não aparece em um app | Tente `paste_keys = "shift+insert"` ou `method = "type"`. O texto continua na área de transferência |
| Transcrição local lenta | `-Build blas` ou `cuda12` no setup; modelo `small`; ou `engine = "groq"` |
| Modo comando diz "precisa de um provedor de LLM" | Adicione uma chave em Chaves de API, ou use o Ollama |
| "A janela mudou: o texto ficou na área de transferência" | Você trocou de janela durante a transcrição: por segurança nada foi colado. Cole com Ctrl+V ou Shift+Alt+Z |
| Configurações, chave ou modelo "somem" ao abrir pelo Menu Iniciar | O Bira Flow foi configurado de dentro do app Claude, que guarda o AppData numa pasta virtual própria. Win+R → `powershell -ExecutionPolicy Bypass -File F:\Whisperflow\scripts\import-claude-data.ps1` copia tudo para as pastas reais |
| Uma palavra sai sempre errada | Adicione uma correção no Dicionário, ou corrija o ditado no Histórico |

Detalhes ficam em `%APPDATA%\BiraFlow\biraflow.log`.

## Desenvolvimento

```powershell
.venv\Scripts\python -m pip install -e ".[dev]"
.venv\Scripts\python -m pytest
.venv\Scripts\python -m biraflow --selftest                  # janela, pílula, ícones, microfone, UIA, bandeja
.venv\Scripts\python -m biraflow --transcribe gravacao.wav   # testa transcrição + limpeza num arquivo
```

Os testes de interface (`tests/test_settings_ui.py`, `tests/test_pill.py`) e o `--selftest` abrem
a janela de configurações em modo invisível (transparente, sem cliques, nunca ativada): rodam com
o app minimizado e sem tirar o foco do que você está usando. Instruções para agentes de código
(Claude, Codex etc.) estão em [AGENTS.md](AGENTS.md).

| Módulo | Função |
|---|---|
| `winhook.py` | Hook de teclado global (só observa; nunca bloqueia o Esc) |
| `hotkey.py` | Leitura do atalho e rastreamento de acordes, repetições e teclas presas |
| `statemachine.py` | Segurar / toque duplo / mãos livres / Esc / parada automática (lógica pura, testada) |
| `controller.py` | Liga hook, máquina de estados, gravador e tarefas de transcrição |
| `audio.py` | Captura do microfone em 16 kHz mono |
| `transcribe.py` | whisper.cpp local, API da Groq ou da OpenAI |
| `cleanup.py` | Limpeza dos artefatos do Whisper, LLM (limpeza, estilos, modo comando) |
| `textproc.py` | Correções, snippets, pontuação falada, limpeza offline, "pressione enter", aprendizado |
| `store.py` | Dicionário, histórico e estatísticas (JSON local) |
| `appcontext.py` | App em foco e categoria de estilo |
| `mediamute.py`, `sounds.py` | Silenciar outros apps (Core Audio) e sons da interface |
| `inserter.py` | Área de transferência (com restauração de todos os formatos), Ctrl+V e digitação |
| `pill.py`, `tray.py` | Pílula flutuante e ícone na bandeja |
| `settings_ui.py` | Janela de configurações (barra lateral, tema Sun Valley / Windows 11) |
| `config.py` | `settings.toml`, `.env`, caminhos e migração do nome antigo |
| `setup_local.py` | Instala o whisper.cpp e baixa modelos pelo app (com progresso e cancelamento) |
| `branding.py`, `assets/` | Logo "b∞ bira flow", ícone do app e ícones da bandeja |
| `editwatch.py` | Aprende correções feitas onde o texto foi colado (UI Automation) e lê o texto antes do cursor |
| `spell.py` | Corretor ortográfico do Windows (separa erro de transcrição de mudança de conteúdo) |
| `whisper_server.py` | whisper-server na placa de vídeo, com descarga após inatividade |
| `audiofile.py`, `audio_ui.py` | Arquivos de áudio e ditados longos em partes; janela "Transcrever áudio" e da reunião |
| `loopback.py` | O que o PC toca (WASAPI loopback do dispositivo padrão e do de comunicações) |
| `meeting.py` | Modo Reunião: gravação em partes, transcrição progressiva, notas e recuperação |
| `updater.py` | Atualização automática assinada (SHA-256 + certificado) |
| `win32.py` | Chamadas do Windows (janelas, teclas, elevação, privacidade do microfone, tela bloqueada) |
