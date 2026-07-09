# YouTube Music AI App — Groq + Shorts/TikTok

App local em Streamlit para transformar uma música em um pacote pronto para YouTube, YouTube Shorts e TikTok.

## Recursos principais

- Upload de áudio.
- Campo de imagem **16:9** para vídeo YouTube normal.
- Campo de imagem **9:16 vertical** para YouTube Shorts e TikTok.
- Os campos de imagem são opcionais por formato:
  - se enviar **só a imagem 16:9**, o app gera **só o vídeo YouTube 16:9**;
  - se enviar **só a imagem 9:16**, o app gera **só o vídeo vertical para Shorts/TikTok**;
  - se enviar as duas imagens, o app gera os dois formatos.
- Upload opcional de thumbnail YouTube 16:9.
- Letra da música em texto ou arquivo `.txt`.
- Opção para incluir a letra no final da descrição do YouTube.
- Geração via **Groq API** de:
  - título, descrição, tags e hashtags do YouTube 16:9;
  - título, descrição e tags para YouTube Shorts;
  - caption e parâmetros de postagem para TikTok;
  - prompts de imagem 16:9 e 9:16;
  - comentário fixado sugerido.
- Regra automática de título: **Nome do Artista - nome música**.
- Renderização automática de:
  - vídeo 16:9 em MP4/H.264;
  - vídeo vertical 9:16 em MP4/H.264 para Shorts/TikTok, com corte automático seguro em `SHORTS_MAX_DURATION_SECONDS`.
- Publicação no YouTube via OAuth.
- Publicação/envio no TikTok via Content Posting API, com leitura automática do token no `.env` ou em `credentials/.tiktok_token.json`.
- Configurações da barra lateral salvas automaticamente em `outputs/ui_settings.json`, mantendo os defaults originais quando ainda não houver configuração salva.
- Proteção opcional para publicar em Streamlit: login por senha (`APP_USERNAME`/`APP_PASSWORD`) e allowlist de IP/CIDR (`APP_ALLOWED_IPS`).
- Campo visível de **data e hora de publicação** logo no início do fluxo, antes de gerar o pacote.
- **Agendamento nativo no YouTube/Shorts**: ao escolher agendamento, o vídeo sobe agora como privado e libera automaticamente no horário escolhido usando `status.publishAt`.
- **TikTok somente por Inbox/Draft**: a aba TikTok envia o vídeo para a Inbox/Caixa de entrada do TikTok e deixa a legenda pronta para copiar/colar ao finalizar ou agendar pelo fluxo nativo do TikTok.

> Importante: use apenas músicas, capas, thumbnails e letras que sejam suas, licenciadas ou autorizadas. O app otimiza metadados, mas não garante viralização.

## Estrutura

```text
youtube_music_ai_app_groq_lyrics/
  app.py
  requirements.txt
  tiktok_oauth_setup.py
  .env.example
  run_windows.bat
  run_mac_linux.sh
  core/
    ai_metadata.py
    config.py
    media.py
    scheduler.py
    tiktok_upload.py
    utils.py
    youtube_upload.py
  credentials/
    .gitkeep
  outputs/
    .gitkeep
  uploads/
    .gitkeep
```

## Pré-requisitos

- Python 3.10 ou superior.
- FFmpeg instalado e disponível no PATH.
- Uma chave da Groq API para gerar metadados por IA.
- Para postar automaticamente no YouTube: projeto no Google Cloud com YouTube Data API habilitada e credenciais OAuth de app desktop.
- Para postar no TikTok: app aprovado/ativo no TikTok for Developers e **User Access Token OAuth** com escopo `video.upload` para Inbox/Draft.

## Limite automático para YouTube Shorts

O vídeo vertical de Shorts/TikTok é limitado por padrão a **179 segundos** (`2:59`). O YouTube permite Shorts de até 3 minutos quando o vídeo é quadrado ou vertical, mas 3 minutos exatos podem virar `180.01s` depois da renderização/metadata do encoder e acabar não sendo classificado como Shorts. Por isso o app usa uma margem de segurança.

Esse corte afeta somente o arquivo vertical `shorts_tiktok_video_9x16.mp4`. O vídeo 16:9 do YouTube continua usando a música completa.

Para alterar o limite, edite no `.env`:

```env
SHORTS_MAX_DURATION_SECONDS=179
```

## Instalar FFmpeg

### Windows

Com Chocolatey:

```powershell
choco install ffmpeg
```

Ou instale manualmente pelo site oficial do FFmpeg e adicione a pasta `bin` ao PATH.

### macOS

```bash
brew install ffmpeg
```

### Linux Ubuntu/Debian

```bash
sudo apt update
sudo apt install ffmpeg
```

## Configuração da Groq API

1. Acesse o console da Groq.
2. Crie uma API key.
3. Copie `.env.example` para `.env`.
4. Preencha:

```env
GROQ_API_KEY=sua_chave_aqui
GROQ_TEXT_MODEL=llama-3.3-70b-versatile
```

Alternativa mais leve/rápida:

```env
GROQ_TEXT_MODEL=llama-3.1-8b-instant
```

Se a chave não estiver configurada ou a API falhar, o app usa um fallback local mais simples.

## Configuração do upload para YouTube

Para os botões de upload YouTube/Shorts funcionarem:

1. Entre no Google Cloud Console.
2. Crie ou escolha um projeto.
3. Habilite a YouTube Data API v3.
4. Configure a tela de consentimento OAuth.
5. Crie uma credencial OAuth do tipo “Desktop app”.
6. Baixe o arquivo JSON.
7. Renomeie para `client_secret.json`.
8. Salve em:

```text
credentials/client_secret.json
```

Na primeira postagem, o app abrirá o navegador para você autorizar a conta do YouTube. Depois disso, o token fica salvo em:

```text
credentials/youtube_token.json
```

Se trocar de conta, apague esse arquivo e autorize novamente.

### Agendamento nativo no YouTube/Shorts

O campo de agendamento fica na etapa **2. Agendamento de publicação**, antes dos dados da música. Escolha:

```text
Agendar data e hora abaixo
```

Depois selecione a **data** e a **hora de publicação/liberação**. Quando você gerar o pacote e clicar para publicar no **YouTube 16:9** ou no **YouTube Shorts**, o app faz upload imediatamente com:

```json
{
  "status": {
    "privacyStatus": "private",
    "publishAt": "DATA_HORA_COM_FUSO"
  }
}
```

Isso deixa o vídeo já enviado no YouTube, privado, aguardando o horário para ficar público. O fuso padrão é:

```env
DEFAULT_TIMEZONE=America/Sao_Paulo
```

Você pode alterar esse valor no `.env` se quiser.

## Configuração do TikTok

No `.env`, você pode preencher `TIKTOK_ACCESS_TOKEN` manualmente ou gerar pelo script OAuth incluído. O app também lê automaticamente o token salvo em `credentials/.tiktok_token.json`, então a tela de envio pode ficar com o campo de token vazio. O fluxo recomendado é:

```bash
python tiktok_oauth_setup.py
```

Antes de rodar o script, configure no `.env`:

```env
TIKTOK_CLIENT_ID=sua_client_key_ou_client_id
TIKTOK_CLIENT_SECRET=seu_client_secret
TIKTOK_REDIRECT_URI=http://localhost:8000/callback
TIKTOK_SCOPES=user.info.basic,video.upload
TIKTOK_DEFAULT_PRIVACY_LEVEL=SELF_ONLY
```

O script abre o navegador, captura o callback local, salva o JSON em `credentials/.tiktok_token.json` e atualiza automaticamente:

```env
TIKTOK_ACCESS_TOKEN=...
TIKTOK_REFRESH_TOKEN=...
TIKTOK_OPEN_ID=...
TIKTOK_TOKEN_FILE=credentials/.tiktok_token.json
```

Na hora de postar, a ordem de busca é: token digitado na tela, `TIKTOK_ACCESS_TOKEN` do `.env`, `TIKTOK_TOKEN_FILE`, `credentials/.tiktok_token.json`, `credentials/tiktok_token.json` e `.tiktok_token.json`. Se houver `refresh_token` e credenciais do app, o backend tenta renovar o token automaticamente quando a API retornar erro de autenticação.

Atenção: o **Client Key** do TikTok sozinho não envia vídeo. Ele é usado no OAuth/Login Kit para obter um **User Access Token** autorizado pelo usuário. Nesta versão, o app usa somente `inbox_upload`, que envia o vídeo para Inbox/Draft usando `/v2/post/publish/inbox/video/init/`; depois você finaliza ou agenda dentro do TikTok.

A Content Posting API de Inbox/Draft aceita o upload do arquivo, mas não aceita caption/descrição no payload desse endpoint. Por isso, se o TikTok mostrar uma hashtag padrão do app, como `#tiktokuploadtest`, substitua pela legenda gerada pela IA que o app exibe e disponibiliza para download.

### Consulta de status do TikTok

Depois de enviar para `inbox_upload`, o app salva o último `publish_id` na sessão e consulta o endpoint `/v2/post/publish/status/fetch/`. Isso ajuda a diferenciar:

- `PROCESSING_UPLOAD`: o TikTok ainda está processando o arquivo;
- `SEND_TO_USER_INBOX`: a notificação foi enviada para a Inbox/Caixa de entrada da conta que autorizou o token;
- `PUBLISH_COMPLETE`: o post foi concluído pelo fluxo do TikTok;
- `FAILED`: o TikTok rejeitou ou falhou no processamento, com `fail_reason` quando disponível.

Se o envio para Inbox/Draft retornar sucesso, mas nada aparecer para você, abra o TikTok no celular na mesma conta que autorizou o OAuth, vá em **Inbox/Caixa de entrada/Notificações** e procure a notificação do upload. Se o status ficar `PROCESSING_UPLOAD`, aguarde e clique em **Consultar status desse publish_id no TikTok**. Se vier `FAILED`, leia o `fail_reason` exibido pelo app.

### TikTok Inbox/Draft e legenda

A aba TikTok usa apenas o envio oficial para Inbox/Draft. Quando houver data/hora escolhida na etapa de agendamento, ela serve como referência visual: o agendamento real deve ser concluído dentro do TikTok, após abrir a notificação da Inbox/Draft.

Fluxo recomendado:

1. gere o pacote;
2. entre na seção **TikTok**;
3. revise a legenda gerada pela IA;
4. clique em **Enviar para TikTok Inbox/Draft**;
5. abra a notificação no TikTok;
6. substitua qualquer hashtag padrão do app pela legenda pronta;
7. finalize ou agende pelo TikTok.

## Saída gerada

Quando os dois formatos são gerados, a pasta fica assim:

```text
outputs/nome-da-musica-xxxxxxx/
  youtube_video_16x9.mp4
  shorts_tiktok_video_9x16.mp4
  manifest.json
  assets/
    audio.mp3
    cover_16x9.jpg
    cover_vertical_9x16.jpg
    thumbnail.jpg
    shorts_tiktok_preview.jpg
```

Quando só um formato é gerado, apenas os arquivos daquele formato aparecem.

## Letra na descrição

A opção **Incluir a letra na descrição do YouTube 16:9** vem marcada por padrão. Quando ela está ativa, o app:

1. gera a descrição otimizada com Groq;
2. anexa ao final a seção:

```text
LETRA DA MÚSICA:
...
```

A descrição final é limitada internamente a 5000 caracteres para evitar erro no upload. Se a letra for maior do que o limite disponível, o app mantém o máximo possível e adiciona um aviso de corte automático.

Use essa função apenas para letras que você tem direito de publicar.

## Variáveis importantes do `.env`

```env
GROQ_API_KEY=
GROQ_TEXT_MODEL=llama-3.3-70b-versatile
GROQ_TEMPERATURE=0.85
GROQ_MAX_TOKENS=2500

YOUTUBE_CLIENT_SECRETS=credentials/client_secret.json
YOUTUBE_TOKEN_FILE=credentials/youtube_token.json

TIKTOK_ACCESS_TOKEN=
TIKTOK_TOKEN_FILE=credentials/.tiktok_token.json
TIKTOK_DEFAULT_PRIVACY_LEVEL=SELF_ONLY
TIKTOK_CLIENT_ID=
TIKTOK_CLIENT_SECRET=
TIKTOK_REDIRECT_URI=http://localhost:8000/callback
TIKTOK_SCOPES=user.info.basic,video.upload
TIKTOK_SCHEDULER_ENABLED=true
TIKTOK_QUEUE_POLL_SECONDS=60

APP_AUTH_ENABLED=true
APP_USERNAME=admin
APP_PASSWORD=troque-por-uma-senha-forte
APP_ALLOWED_IPS=189.120.78.7
APP_IP_STRICT=true

DEFAULT_TIMEZONE=America/Sao_Paulo

VIDEO_WIDTH=1920
VIDEO_HEIGHT=1080
SHORTS_VIDEO_WIDTH=1080
SHORTS_VIDEO_HEIGHT=1920
SHORTS_MAX_DURATION_SECONDS=179
VIDEO_FPS=30
VIDEO_CRF=18
```

## Central TikTok, fila e status

A versão atual inclui uma central de status no topo do app para acompanhar cada agendamento TikTok. Ela mostra:

- itens agendados, em envio, aguardando confirmação do TikTok, confirmados e com erro;
- `publish_id` retornado pela API;
- `tiktok_status` consultado na API oficial;
- linha do tempo de eventos por item;
- erro bruto quando o TikTok recusar o upload, o token estiver sem escopo ou o arquivo local tiver sido apagado;
- ações rápidas para processar vencidos agora, atualizar status, publicar imediatamente, tentar novamente, cancelar e remover itens antigos da lista.

Importante: no Inbox/Draft, o upload aceito ainda precisa ser finalizado no TikTok. O app registra o `publish_id`, consulta status e considera confirmado quando o TikTok retorna `SEND_TO_USER_INBOX` ou outro status final de sucesso. Se retornar `FAILED`, o item fica como erro com o motivo retornado pela plataforma.

## Streamlit Cloud e agendamentos TikTok

No Streamlit Cloud, o app pode dormir quando não há tráfego. Se ele estiver dormindo exatamente no horário do agendamento, a fila só será processada quando o app acordar novamente. Para reduzir esse risco, configure um token de ping nos Secrets:

```toml
SCHEDULER_PING_TOKEN = "crie-um-token-longo-e-aleatorio"
```

Depois crie um monitor externo, como UptimeRobot, Better Stack ou cron externo, chamando periodicamente:

```text
https://SEU-APP.streamlit.app/?queue_token=crie-um-token-longo-e-aleatorio
```

Esse ping não abre a interface: ele só executa uma rodada segura de manutenção da fila, processa itens vencidos e consulta status pendentes.

### Teste experimental da Share Video API legada

A seção TikTok também inclui um expander opcional **“Teste experimental: Share Video API legada/descontinuada”**. Ele chama o endpoint antigo `https://open-api.tiktok.com/share/video/upload/`, que o próprio TikTok marcou como descontinuado. Use apenas para teste.

Limitações desse modo legado:

- exige `access_token` e `open_id` do usuário TikTok;
- aceita apenas MP4 de até 50 MB;
- não possui campo oficial de caption/descrição;
- pode falhar porque o endpoint foi descontinuado pelo TikTok.

Para testar, deixe `TIKTOK_OPEN_ID` nos Secrets/.env ou garanta que `credentials/.tiktok_token.json` tenha o campo `open_id`.
