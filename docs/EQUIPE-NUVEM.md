# Bira Flow em equipe, tudo em nuvem

Guia curto para instalar e operar o Bira Flow nos computadores da equipe **sem o modelo local**
(nada de whisper.cpp, nada de GPU, nada de download de 500 MB a 1,5 GB). Transcrição pela **Groq**
e limpeza do texto, notas da reunião e leitura de print pelo **Vercel AI Gateway**.

Para o modo Reunião e o resto do recurso, o plano completo é `docs/PLANO-REUNIOES.md`; o resumo da
última sessão de trabalho é `docs/SESSAO-2026-10-18.md`.

---

## 1. O que cada PC precisa

| | |
|---|---|
| Windows | 10 ou 11 (x64) |
| Permissão | nenhuma: o script copia a pasta para `%LOCALAPPDATA%\Programs\Bira Flow` (sem administrador) |
| Internet | sim (a transcrição acontece na Groq) |
| Microfone | o que o PC já tem |
| Chaves | `GROQ_API_KEY` (transcrição) e `AI_GATEWAY_API_KEY` (limpeza, notas, print) |

Não precisa de GPU e não é preciso rodar `scripts\setup-whisper.ps1`.

## 2. Instalação num PC da equipe

**A forma da equipe é a pasta portátil:** um zip de **~55 MB** (`BiraFlow-1.1.0-portable.zip`),
sem instalador, sem certificado e sem aviso do Windows. Descompacte num lugar de rede (por exemplo
`\\servidor\bira\BiraFlow`) e rode o script em cada PC — ele copia o app para
`%LOCALAPPDATA%\Programs\Bira Flow`, cria o atalho no Menu Iniciar, grava as chaves e confere:

```powershell
powershell -ExecutionPolicy Bypass -File setup-team.ps1 `
    -SourceDir \\servidor\bira\BiraFlow `
    -EnvFile \\servidor\bira\.env `
    -Autostart
```

`-Autostart` liga "iniciar com o Windows"; sem ele, só o atalho é criado. `-NoShortcut` não cria
atalho nenhum (útil para testar).

O script faz quatro coisas e pode ser rodado de novo sem medo:

1. copia a pasta do app (fechando a versão que estiver aberta) e cria o atalho;
2. grava as chaves em `%APPDATA%\BiraFlow\.env` (ou copia o `.env` que você passou);
3. ajusta `%APPDATA%\BiraFlow\settings.toml`: `engine = "auto"` e `cleanup.provider = "vercel"`;
4. confere com `biraflow-cli.exe --check` e diz o que ficou.

Na mão, o mesmo resultado: descompactar o zip em qualquer pasta, rodar `biraflow.exe` de dentro
dela, colar as duas chaves na aba **Chaves de API** e escolher **Transcrição: Groq ou OpenAI (pela
internet)** e **Limpeza: Vercel AI Gateway**.

**Uma chave só (tudo no Gateway)?** Dá: com apenas `AI_GATEWAY_API_KEY` no `.env`, o motor
`auto` já transcreve pelo Gateway (ou fixe `-Engine gateway` / `engine = "gateway"`). O modelo
padrão é `spacexai/grok-stt`; a reunião fica melhor que pela Groq (tempos por palavra), mas o
ditado longo é mais lento — veja os números no §4.

Também existe o caminho do instalador (`-Installer BiraFlow-Setup-1.1.0.exe`) para quando houver um
build assinado; aí sim há atualização automática.

## 3. O que sobe para a nuvem

| O quê | Para onde | Quando |
|---|---|---|
| Áudio do ditado (Ogg/Opus) | Groq `whisper-large-v3-turbo` | a cada ditado |
| Áudio da reunião, em pedaços de ~90 s | Groq | durante a reunião, depois do consentimento (LGPD) na tela |
| Texto transcrito | Vercel AI Gateway (`google/gemini-2.5-flash-lite`) | limpeza, notas e leitura de print |

Nada é guardado por nós: a Groq e o Gateway processam e devolvem. As gravações da reunião ficam no
PC em `Reuniões` (a pasta de dados) enquanto a reunião acontece e são apagadas depois de
transcritas, conforme a configuração.

Sem chave nenhuma, **nada** é enviado: o app continua funcionando com o modelo local, se ele
estiver instalado.

## 4. Custos e limites

- **Groq**: ~US$ 0,04 por hora de áudio no `whisper-large-v3-turbo`. Uma pessoa que dita 1 h por dia
  gasta ~US$ 0,04/dia. A conta grátis tem limite de requisições por minuto **por modelo**; quando
  estoura, o app tenta o `whisper-large-v3` (mesma qualidade, cota própria) e reenvia depois.
- **Vercel AI Gateway**: a limpeza e as notas usam poucos milhares de tokens por ditado — custo
  desprezível perto da transcrição, mas depende do plano/créditos da conta.
- **Chave compartilhada (a escolha desta equipe)**: todos os PCs usam a mesma `GROQ_API_KEY`, então
  o limite de minutos/requisições da Groq é da equipe inteira. Para trocar a chave (ou se ela
  vazar), gere outra no painel da Groq e rode o `setup-team.ps1` de novo em cada PC com o `.env`
  novo — leva segundos e não mexe em mais nada.
- **Se quiser uma chave paga para tudo**: o Vercel AI Gateway também transcreve, e o app já tem
  motor para isso (**`engine = "gateway"`**). Medido em 18/10 com `spacexai/grok-stt`
  (US$ 0,10/h): **100 % de âncoras** nos três áudios de teste e, na reunião, **100 % de falantes e
  100 % de minutagem** (melhor que a Groq, que fica em 97,5 %/96,4 %); em troca, o ditado longo é 2
  a 4× mais devagar que a Groq (5 min: 8-14 s; 31 min: 35-53 s) e varia com a rede. Com uma chave
  só, `engine = "auto"` já escolhe o Gateway quando não há chave da Groq.
  Ao usar o Gateway, o áudio do ditado e da reunião sobe para ele — não para a Groq.

## 5. Sem internet, ou com a API fora do ar

O ditado não se perde: o áudio fica em `%APPDATA%\BiraFlow\falhas` (os 3 últimos, por 24 h) e volta
pelo menu da bandeja, em **Transcrever de novo o último áudio**. A reunião já transcrita continua na
pasta dela e termina sozinha quando a conexão voltar.

Se quiser tolerância a internet ruim, dá para manter o modelo local como reserva
(`fallback_to_local`); é opcional e não é preciso para a equipe.

## 6. Atualizar

**Não existe instalador — é de propósito** (opção escolhida em 18/10): distribuir um instalador sem
assinatura faria o Windows avisar "editor desconhecido" em cada PC, e o instalador só traz vantagem
real quando é assinado (aí sim com atualização automática). O caminho da equipe é a pasta portátil.

Para levar uma versão nova à equipe:

1. gere/receba o zip novo (`dist\BiraFlow-<versão>-portable.zip`) e deixe na pasta de rede;
2. em cada PC, rode de novo o `setup-team.ps1 -SourceDir ...` com o `.env`.

O script fecha o app que estiver aberto, apaga o `_internal` velho, copia por cima e **mantém
chaves, configuração, dicionário, histórico e reuniões** (eles ficam em `%APPDATA%\BiraFlow`).
As chaves são o ponto sensível: o script guarda o `.env` anterior em `.env.bak` e, ao aplicar um
`.env` novo, **mantém toda chave que o arquivo novo não traga** (e diz quais foram mantidas) — um
arquivo velho não derruba a chave que estava funcionando.

Se um dia quiserem atualização automática, o `installer.iss` e os scripts de assinatura estão no
repositório: basta assinar o build (com o PFX do outro PC, ou com um certificado novo confiado em
cada PC por `trust-signing-cert.ps1`) e publicar com `scripts\publish-release.ps1`.

## 7. Conferir num PC

```powershell
& "$env:LOCALAPPDATA\Programs\Bira Flow\biraflow-cli.exe" --check
```

O esperado num PC em nuvem:

```
  GROQ_API_KEY      : definida
  AI_GATEWAY_API_KEY: definida
  transcrição   : groq
  modelo local  : ... (NÃO ENCONTRADO)      <- normal: não é usado
  limpeza LLM   : vercel
```

Depois, um teste rápido de verdade: abrir o app, segurar `Ctrl+Win`, falar uma frase e soltar. Se o
texto aparecer, o caminho da nuvem está de pé.

## 8. Modo Reunião numa máquina fraca

O que roda no PC: gravar os dois canais (microfone e áudio do PC), codificar cada pedaço de ~90 s em
Opus e subir. Medido nesta máquina: ~0,5 s de CPU por pedaço de 90 s, e o ditado longo codifica os
pedaços de 10 min em paralelo enquanto o anterior sobe (um ditado de 31 min leva ~20 s no total,
contando o envio e a transcrição). Não há processamento de modelo no PC.

Um PC fraco, portanto, precisa apenas de memória para o app aberto (Python + áudio) e de banda para
subir ~1 MB por 5 min de áudio.
