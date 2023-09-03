import logging
import io
import os
import uuid
import requests
import json
import quickstart

from quickstart import upload_image_to_blob, create_upload_file
from fastapi.responses import Response
from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Form, Depends
from azure.storage.blob import BlobServiceClient
from starlette.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
from aioredis import Redis
from twilio.request_validator import RequestValidator
from typing import Optional
from twilio.twiml.messaging_response import MessagingResponse
from typing import Optional, List
from typing import Dict

app = FastAPI(debug=True)

import dai
import aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Initialization
connection_string = "DefaultEndpointsProtocol=https;AccountName=wingmanblobstorage;AccountKey=YOUR_KEY_HERE;EndpointSuffix=core.windows.net"
blob_service_client = BlobServiceClient.from_connection_string(connection_string)
container_name = "conversations"

# Questions and Models
preset_questions = [
    "What activity is she currently engaged in?",
    "Describe her facial expression or mood:",
    "How would you describe her style today?",
    "What are notable aspects of her attire or accessories?",
    "What initially caught your attention about her?",
    "How is she positioned in the setting?",
    "Can you guess her current emotional state?",
    "Do you observe any interesting non-verbal cues?",
    "Any additional insights not captured in the photo or above questions?"
]

class Message(BaseModel):
    user_id: str
    message: str

class Conversation(BaseModel):
    user_id: str
    conversation_id: Optional[str] = None
    messages: List[str] = []

class AzureConversationLogger:
    def __init__(self, user_id, blob_service_client, container_name):
        self.user_id = user_id
        self.blob_service_client = blob_service_client
        self.container_name = container_name
        self.blob_client = self.blob_service_client.get_blob_client(container=self.container_name, blob=f"{self.user_id}.txt")
        if not self.blob_client.exists():
            logger.info(f"Blob for user {self.user_id} does not exist. Creating...")
            self.blob_client.create_append_blob()

    def log_message(self, role, message):
        self.blob_client.append_block(f"{role}: {message}\n")

class PickupLineConversation(BaseModel):
    conversation_id: str
    user_id: str
    image_url: Optional[str] = None
    questions: List[str] = []
    answers: List[str] = []
    pickup_lines: List[str] = []
    messages: List[Dict[str, str]] = []

# Database Initializations
app.conversations_db: Dict[str, Dict[str, List[str]]] = {}
app.pickup_line_conversations_db: Dict[str, PickupLineConversation] = {}

# Redis State Management
async def get_or_init_user_state(user_id: str) -> str:
    state = await app.redis.get(f"{user_id}-state")
    if state:
        return state.decode("utf-8")
    await app.redis.set(f"{user_id}-state", "START")
    return "START"

async def set_user_state(user_id: str, state: str):
    await app.redis.set(f"{user_id}-state", state)

# App Events
@app.on_event("startup")
async def startup_event():
    try:
        app.redis = await Redis.from_url(os.getenv('REDIS_CONNECTION_STRING'))
        logging.info("Successfully connected to Redis.")
    except Exception as e:
        logging.error(f"Error connecting to Redis: {str(e)}")

@app.on_event("shutdown")
async def shutdown_event():
    await app.redis.close()

# Root Endpoint
@app.get("/")
async def root():
    return {"message": "Welcome to Wingman AI!"}

@app.post("/upload/{user_id}")
async def image_upload(user_id: str, image: UploadFile = File(...)):
    conversation_id = str(uuid.uuid4())
    try:
        if not image.filename:
            raise HTTPException(status_code=400, detail="No file provided.")
        
        if not any(image.filename.endswith(ext) for ext in ['.png', '.jpg', '.jpeg', '.gif']):
            raise HTTPException(status_code=400, detail="Unsupported file type. Supported types are: .png, .jpg, .jpeg, .gif")
        
        file_content = await image.read()
        await image.seek(0)

        try:
            Image.open(io.BytesIO(file_content))
        except IOError:
            raise HTTPException(
                status_code=400,
                detail="Invalid file type. Please upload an image file."
            )

        if len(file_content) > 30e6:
            raise HTTPException(
                status_code=400,
                detail="Image file size is too large. Please upload a smaller image."
            )

        image_url = await quickstart.upload_image_to_blob(image)
        logging.info(f"Successfully uploaded image to blob storage: {image_url}")
        
        # Check if the upload was successful
        if not image_url:
            raise HTTPException(
                status_code=500,
                detail="Failed to upload image to blob storage."
            )
        
        await app.redis.set(f"{conversation_id}-image_url", image_url)

        question_to_ask = preset_questions[0]
        await app.redis.set(f"{conversation_id}-question_index", "1")
        await app.redis.set(f"{conversation_id}-question", question_to_ask)

        pickup_line_convo = PickupLineConversation(
            conversation_id=conversation_id,
            user_id=user_id,
            image_url=image_url,
            questions=[question_to_ask],
            answers=[]
        )
        app.pickup_line_conversations_db[conversation_id] = pickup_line_convo
        return {"message": "Upload successful.", "question_id": 0, "question": question_to_ask, "conversation_id": conversation_id}

    except HTTPException as e:
        logging.error(f"HTTP Exception occurred in image_upload: {e.detail}")
        raise
    except Exception as e:
        logging.error(f"Unexpected error occurred in image_upload: {str(e)}")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/answer/{conversation_id}/{question_id}")
async def answer_question(conversation_id: str, question_id: int, answer: str):
    question_index_raw = await app.redis.get(f"{conversation_id}-question_index")
    if question_index_raw is None:
        raise HTTPException(status_code=404, detail="No question found to answer.")
    question_index = int(question_index_raw.decode("utf-8"))
    if question_index != question_id + 1:
        raise HTTPException(status_code=404, detail="No question found to answer.")

    question_to_ask = None
    more_questions = False
    if question_index < len(preset_questions):
        question_to_ask = preset_questions[question_index]
        await app.redis.set(f"{conversation_id}-question_index", str(question_index + 1))
        await app.redis.set(f"{conversation_id}-question", question_to_ask)
        app.pickup_line_conversations_db[conversation_id].questions.append(question_to_ask)
        more_questions = True

    # Store the answer in Redis
    await app.redis.lpush(f"{conversation_id}-answers", answer)
   
    # Store the answer in the pickup line conversations database
    app.pickup_line_conversations_db[conversation_id].answers.append(answer)

    # Check if more questions are available
    if more_questions:
        return {
            "message": f"Thanks for your answer! Here's the next question: {question_to_ask}",
            "more_questions": more_questions,
            "next_question": question_to_ask
        }
    else:
        return {
            "message": "Thank you for answering all the questions!",
            "more_questions": more_questions,
            "next_question": None
        }

    return {"message": "Answer processed successfully.", "more_questions": more_questions, "next_question": question_to_ask}

@app.post("/generate/{conversation_id}")
async def generate_statements(conversation_id: str):
    session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
    if session_id_raw is None:
        raise HTTPException(status_code=404, detail="No active session found for this conversation.")
    session_id = session_id_raw.decode("utf-8")

    situation = (await app.redis.get(f"{session_id}-situation")).decode("utf-8")
    history = json.loads((await app.redis.get(f"{session_id}-history")).decode("utf-8"))
   
    # Retrieve answers
    answers = app.pickup_line_conversations_db[conversation_id].answers

    # Pass answers to the function that generates pickup lines
    pickup_lines = await dai.generate_pickup_lines(situation, history, answers, 5)

    app.pickup_line_conversations_db[conversation_id].pickup_lines = pickup_lines

    return {"pickup_line": pickup_lines}

@app.post("/process-command/{conversation_id}/{command}")
async def process_command(conversation_id: str, command: str) -> Dict[str, str]:
    try:
        # Check if the conversation_id exists in the pickup_line_conversations_db
        if conversation_id not in app.pickup_line_conversations_db:
            raise HTTPException(status_code=404, detail="Conversation not found.")
       
        # Check if the command is properly formed
        if not command:
            raise HTTPException(status_code=400, detail="Invalid command.")
       
        # Fetch the conversation history and pickup lines
        history = app.pickup_line_conversations_db[conversation_id].messages.copy()
        pickup_lines = app.pickup_line_conversations_db[conversation_id].pickup_lines.copy()

        # Process the user's command
        response = await dai.process_user_query(command, history, pickup_lines)

        # Update the conversation history with ONLY the user's command and the assistant's response
        app.pickup_line_conversations_db[conversation_id].messages.extend([
            {"role": "user", "content": command},
            {"role": "assistant", "content": response}
        ])

        return {"assistant_response": response}

    except HTTPException as e:
        return JSONResponse(status_code=e.status_code, content={"message": e.detail})
    except Exception as e:
        logger.error(f"Unexpected error: {str(e)}")
        return JSONResponse(status_code=500, content={"message": "An unexpected error occurred."})

@app.get("/pickup-line-conversations/{user_id}")
async def get_pickup_line_conversations(user_id: str):
    return {
        "pickup_line_conversations": [
            convo.dict() for convo in app.pickup_line_conversations_db.values() if convo.user_id == user_id
        ]
    }

@app.get("/pickup-line-conversation/{conversation_id}")
async def get_pickup_line_conversation(conversation_id: str):
    if conversation_id not in app.pickup_line_conversations_db:
        raise HTTPException(status_code=404, detail="Pickup line conversation not found.")
    return {"pickup_line_conversation": app.pickup_line_conversations_db[conversation_id].dict()}

@app.post("/twilio-webhook/")
async def twilio_webhook(request: Request):
    try:
        form_data = await request.form()

        # Logging for better debugging
        logging.info(f"Received data from Twilio: {form_data}")

        incoming_msg = form_data.get('Body').strip()
        from_number = form_data.get('From').strip()
        user_id = from_number

        resp = MessagingResponse()
        msg = resp.message()

        user_state = await get_or_init_user_state(user_id)

        if user_state == "START":
            msg.body("Welcome to Wingman AI! Please upload an image to get started or type 'skip' to proceed without an image.")
            await set_user_state(user_id, "AWAITING_IMAGE")
        elif user_state == "AWAITING_IMAGE":
            if incoming_msg.lower() == "skip":
                response = await start_questions_without_image(user_id=user_id)
                msg.body(response['first_question'])
                await set_user_state(user_id, f"QUESTION_0")
            elif "MediaUrl0" in form_data:
                image_url = form_data.get('MediaUrl0')
                logging.info(f"Extracted Image URL from Twilio in twilio_webhook: {image_url}")
                response = await image_upload(user_id=user_id, image_url=image_url)
                response_data = json.loads(response.body.decode("utf-8"))
                question_text = response_data.get('message', "No message available.")
                msg.body(question_text)
                await set_user_state(user_id, f"QUESTION_{response_data['question_id']}")
            else:
                msg.body("Please upload an image to proceed or type 'skip' to proceed without an image.")
        elif user_state.startswith("QUESTION_"):
            question_id = int(user_state.split("_")[1])
            response = await answer_question(conversation_id=user_id, question_id=question_id, answer=incoming_msg)
            msg.body(response['message'])
            if response['more_questions']:
                await set_user_state(user_id, f"QUESTION_{question_id + 1}")
            else:
                await set_user_state(user_id, "AWAITING_COMMAND")
        elif user_state == "AWAITING_COMMAND":
            response = await process_command(conversation_id=user_id, command=incoming_msg)
            msg.body(response['assistant_response'])
            await set_user_state(user_id, "AWAITING_NEXT_ACTION")
        elif user_state == "AWAITING_NEXT_ACTION":
            msg.body("Thank you for using Wingman AI! If you'd like to start over, please type 'restart'.")
            if incoming_msg.lower() == "restart":
                await set_user_state(user_id, "START")
                msg.body("Welcome back to Wingman AI! Please upload an image to get started or type 'skip' to proceed without an image.")
            else:
                msg.body("If you'd like to start over, please type 'restart'.")
        else:
            logging.error(f"Unhandled user state in twilio_webhook: {user_state}")
            msg.body("Apologies, something went wrong. Please try again later.")

        return str(resp)

    except Exception as e:
        logging.error(f"Unexpected error in twilio_webhook: {str(e)}")
        raise

@app.get("/conversations/{user_id}")
async def get_conversations(user_id: str):
    conversations = app.conversations_db.get(user_id, [])
    return {"conversations": conversations}

async def start_questions_without_image(user_id: str):
    question_to_ask = preset_questions[0]
    pickup_line_convo = PickupLineConversation(
        conversation_id=user_id,
        user_id=user_id,
        image_url=None,
        questions=[question_to_ask],
        answers=[]
    )
    app.pickup_line_conversations_db[user_id] = pickup_line_convo
    return {"first_question": question_to_ask}

