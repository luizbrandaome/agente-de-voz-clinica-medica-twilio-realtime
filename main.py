import os
import json
import base64
import asyncio
import websockets
from fastapi import FastAPI, WebSocket, Request
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.websockets import WebSocketDisconnect
from twilio.twiml.voice_response import VoiceResponse, Connect, Say, Stream
from dotenv import load_dotenv
import httpx  # Nova biblioteca para requisições HTTP
import ssl
from httpx import AsyncClient
from datetime import datetime
import pytz  # Biblioteca para fusos horários

load_dotenv()

# Tradução manual dos meses
MESES_EM_PORTUGUES = {
    1: "janeiro",
    2: "fevereiro",
    3: "março",
    4: "abril",
    5: "maio",
    6: "junho",
    7: "julho",
    8: "agosto",
    9: "setembro",
    10: "outubro",
    11: "novembro",
    12: "dezembro"
}


# Função para obter a data formatada no fuso horário GMT-3
def obter_data_formatada():
    tz = pytz.timezone("America/Sao_Paulo")
    agora = datetime.now(tz)
    dia = agora.day
    mes = MESES_EM_PORTUGUES[agora.month]
    ano = agora.year
    return f"{dia} de {mes} de {ano}"  # Exemplo: "19 de janeiro de 2025"


# Configuration
OPENAI_API_KEY = os.getenv('OPENAI_API_KEY')
PORT = int(os.getenv('PORT', 5050))

# Define a variável base para o SYSTEM_MESSAGE
system_message_base = (
    f"Você é uma assistente telefônica da clínica Modelo. Nessa clínica atendem os profissionais listados no XML abaixo. Hoje é dia {obter_data_formatada()}, e você deve receber ligações de pessoas com intenção de marcar consulta com um dos profissionais, ou ambos... você pode fornecer duas datas disponíveis por vez e perguntar se alguma delas é de interesse do usuário, se não for, pode oferecer alguma outra data, como podem ter vários dias com vários horários cada, você pode começar perguntando se prefere pela manhã ou pela tarde, e baseado nisso, sugerir horários livres que estão na lista abaixo."
    "A clínica modelo fica situada na Avenida Dom Pedro II n 750, em São Lourenço, Minas Gerais."
    "Pergunte o nome completo do cliente caso ele queira marcar uma consulta;"
    "Pergunte também se o numero de telefone para contato é o mesmo que ele usou para ligar."
    "Caso na mesma ligação o cliente queira marcar uma outra consulta para outra pessoa, lembre de perguntar o nome da outra pessoa também."
    "Confirme se a consulta será por algum plano de saúde, ou se será particular, ou se será retorno."
    "<rules> Ao sugerir datas, se a data for no mesmo mês atual, pode responder somente com o Dia sem mencionar o mês. fale dia e mês somente quando for para mês diferente do atual."
    "Faça somente uma pergunta por vez."
    "Seu idioma é PT-BR"
    "Após o usuário informar o nome completo, você pode chamá-lo posteriormente somente pelo primeiro nome."
    "Caso o numero de telefone não seja o mesmo que o cliente usou para ligar, pergunte se o numero de telefone. </rules>"
)

# SYSTEM_MESSAGE será atualizado dinamicamente antes de ser usado
SYSTEM_MESSAGE = system_message_base  # Inicializa como a base

VOICE = 'coral'
LOG_EVENT_TYPES = [
    'error', 'response.content.done', 'rate_limits.updated', 'response.done',
    'input_audio_buffer.committed', 'input_audio_buffer.speech_stopped',
    'input_audio_buffer.speech_started', 'session.created'
]
SHOW_TIMING_MATH = False

app = FastAPI()

if not OPENAI_API_KEY:
    raise ValueError(
        'Missing the OpenAI API key. Please set it in the .env file.')


async def fetch_text_from_url(url: str) -> str:
    """Fetch text from a given HTTPS URL, ignoring SSL certificate and hostname errors."""
    try:
        # Criar contexto SSL personalizado que ignora validações de certificado e hostname
        ssl_context = ssl.create_default_context()
        ssl_context.check_hostname = False
        ssl_context.verify_mode = ssl.CERT_NONE

        # Usar cliente HTTPX com contexto SSL personalizado
        async with AsyncClient(verify=ssl_context) as client:
            response = await client.get(url)
            response.raise_for_status()
            return response.text.strip()
    except Exception as e:
        print(f"Error while fetching data from {url}: {e}")
        return ""


@app.get("/", response_class=JSONResponse)
async def index_page():
    return {"message": "Twilio Media Stream Server is running!"}


@app.api_route("/incoming-call", methods=["GET", "POST"])
async def handle_incoming_call(request: Request):
    """Handle incoming call and return TwiML response to connect to Media Stream."""
    # URL da qual o texto será buscado
    text_url = "https://srv658237.hstgr.cloud/clinica.php"  # Substitua pelo URL real

    # Recupera o texto da URL e atualiza SYSTEM_MESSAGE dinamicamente
    fetched_text = await fetch_text_from_url(text_url)
    global SYSTEM_MESSAGE
    SYSTEM_MESSAGE = system_message_base  # Sempre parte da base
    if fetched_text:
        SYSTEM_MESSAGE += f"\n{fetched_text}"
        print(f"SYSTEM_MESSAGE atualizado dinamicamente: {SYSTEM_MESSAGE}")

    response = VoiceResponse()
    response.say(
        "Clínica modelo, posso ajudar?",
        voice=
        "alice",  # Use a voz "alice" que suporta múltiplos idiomas, incluindo português
        language="pt-BR"  # Configura o idioma para Português do Brasil
    )
    host = request.url.hostname
    connect = Connect()
    connect.stream(url=f'wss://{host}/media-stream')
    response.append(connect)
    return HTMLResponse(content=str(response), media_type="application/xml")


@app.websocket("/media-stream")
async def handle_media_stream(websocket: WebSocket):
    """Handle WebSocket connections between Twilio and OpenAI."""
    print("Client connected")
    await websocket.accept()

    async with websockets.connect(
            'wss://api.openai.com/v1/realtime?model=gpt-4o-realtime-preview',
            extra_headers={
                "Authorization": f"Bearer {OPENAI_API_KEY}",
                "OpenAI-Beta": "realtime=v1"
            }) as openai_ws:
        await initialize_session(openai_ws)

        # Connection specific state
        stream_sid = None
        latest_media_timestamp = 0
        last_assistant_item = None
        mark_queue = []
        response_start_timestamp_twilio = None

        async def receive_from_twilio():
            """Receive audio data from Twilio and send it to the OpenAI Realtime API."""
            nonlocal stream_sid, latest_media_timestamp
            try:
                async for message in websocket.iter_text():
                    data = json.loads(message)
                    if data['event'] == 'media' and openai_ws.open:
                        latest_media_timestamp = int(
                            data['media']['timestamp'])
                        audio_append = {
                            "type": "input_audio_buffer.append",
                            "audio": data['media']['payload']
                        }
                        await openai_ws.send(json.dumps(audio_append))
                    elif data['event'] == 'start':
                        stream_sid = data['start']['streamSid']
                        print(f"Incoming stream has started {stream_sid}")
                        response_start_timestamp_twilio = None
                        latest_media_timestamp = 0
                        last_assistant_item = None
                    elif data['event'] == 'mark':
                        if mark_queue:
                            mark_queue.pop(0)
            except WebSocketDisconnect:
                print("Client disconnected.")
                if openai_ws.open:
                    await openai_ws.close()

        async def send_to_twilio():
            """Receive events from the OpenAI Realtime API, send audio back to Twilio."""
            nonlocal stream_sid, last_assistant_item, response_start_timestamp_twilio
            try:
                async for openai_message in openai_ws:
                    response = json.loads(openai_message)
                    if response['type'] in LOG_EVENT_TYPES:
                        print(f"Received event: {response['type']}", response)

                    if response.get(
                            'type'
                    ) == 'response.audio.delta' and 'delta' in response:
                        audio_payload = base64.b64encode(
                            base64.b64decode(
                                response['delta'])).decode('utf-8')
                        audio_delta = {
                            "event": "media",
                            "streamSid": stream_sid,
                            "media": {
                                "payload": audio_payload
                            }
                        }
                        await websocket.send_json(audio_delta)

                        if response_start_timestamp_twilio is None:
                            response_start_timestamp_twilio = latest_media_timestamp
                            if SHOW_TIMING_MATH:
                                print(
                                    f"Setting start timestamp for new response: {response_start_timestamp_twilio}ms"
                                )

                        # Update last_assistant_item safely
                        if response.get('item_id'):
                            last_assistant_item = response['item_id']

                        await send_mark(websocket, stream_sid)

                    # Trigger an interruption. Your use case might work better using `input_audio_buffer.speech_stopped`, or combining the two.
                    if response.get(
                            'type') == 'input_audio_buffer.speech_started':
                        print("Speech started detected.")
                        if last_assistant_item:
                            print(
                                f"Interrupting response with id: {last_assistant_item}"
                            )
                            await handle_speech_started_event()
            except Exception as e:
                print(f"Error in send_to_twilio: {e}")

        async def handle_speech_started_event():
            """Handle interruption when the caller's speech starts."""
            nonlocal response_start_timestamp_twilio, last_assistant_item
            print("Handling speech started event.")
            if mark_queue and response_start_timestamp_twilio is not None:
                elapsed_time = latest_media_timestamp - response_start_timestamp_twilio
                if SHOW_TIMING_MATH:
                    print(
                        f"Calculating elapsed time for truncation: {latest_media_timestamp} - {response_start_timestamp_twilio} = {elapsed_time}ms"
                    )

                if last_assistant_item:
                    if SHOW_TIMING_MATH:
                        print(
                            f"Truncating item with ID: {last_assistant_item}, Truncated at: {elapsed_time}ms"
                        )

                    truncate_event = {
                        "type": "conversation.item.truncate",
                        "item_id": last_assistant_item,
                        "content_index": 0,
                        "audio_end_ms": elapsed_time
                    }
                    await openai_ws.send(json.dumps(truncate_event))

                await websocket.send_json({
                    "event": "clear",
                    "streamSid": stream_sid
                })

                mark_queue.clear()
                last_assistant_item = None
                response_start_timestamp_twilio = None

        async def send_mark(connection, stream_sid):
            if stream_sid:
                mark_event = {
                    "event": "mark",
                    "streamSid": stream_sid,
                    "mark": {
                        "name": "responsePart"
                    }
                }
                await connection.send_json(mark_event)
                mark_queue.append('responsePart')

        await asyncio.gather(receive_from_twilio(), send_to_twilio())


async def send_initial_conversation_item(openai_ws):
    """Send initial conversation item if AI talks first."""
    initial_conversation_item = {
        "type": "conversation.item.create",
        "item": {
            "type":
            "message",
            "role":
            "user",
            "content": [{
                "type":
                "input_text",
                "text":
                "Comece dizendo 'Clínica modelo, posso ajudar?'"
            }]
        }
    }
    await openai_ws.send(json.dumps(initial_conversation_item))
    await openai_ws.send(json.dumps({"type": "response.create"}))


async def initialize_session(openai_ws):
    """Control initial session with OpenAI."""
    session_update = {
        "type": "session.update",
        "session": {
            "turn_detection": {
                "type": "server_vad",
                "threshold": 0.4,
                "prefix_padding_ms": 500,
                "silence_duration_ms": 300
            },
            "input_audio_format": "g711_ulaw",
            "output_audio_format": "g711_ulaw",
            "voice": VOICE,
            "instructions": SYSTEM_MESSAGE,
            "modalities": ["text", "audio"],
            "temperature": 0.8,
        }
    }
    print('Sending session update:', json.dumps(session_update))
    await openai_ws.send(json.dumps(session_update))

    # Uncomment the next line to have the AI speak first
    # await send_initial_conversation_item(openai_ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=PORT)
