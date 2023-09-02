import logging
import io
import os
import uuid
import requests
import json

from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Form, Depends
from azure.storage.blob import BlobServiceClient
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
from aioredis import Redis
from twilio.request_validator import RequestValidator
from typing import Optional
from twilio.twiml.messaging_response import MessagingResponse
from typing import Optional, List
from typing import Dict


import dai
import aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(debug=True)

# Initialization
connection_string = "DefaultEndpointsProtocol=https;AccountName=wingmanblobstorage;AccountKey=YOUR_KEY_HERE;EndpointSuffix=core.windows.net"
blob_service_client = BlobServiceClient.from_connection_string(connection_string)
container_name = "conversations"

origins = ["*"]
app.add_middleware(
    CORSMiddleware,
    allow_origins=origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

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
    app.redis = await Redis.from_url(os.getenv('REDIS_CONNECTION_STRING'))

@app.on_event("shutdown")
async def shutdown_event():
    await app.redis.close()

# Root Endpoint
@app.get("/")
async def root():
    return {"message": "Welcome to Wingman AI!"}
    
# ====== Conversation Endpoints ======
@app.post("/conversation/")
async def create_conversation(user_id: str) -> Dict[str, str]:
    conversation_id = str(uuid.uuid4())
    app.conversations_db[conversation_id] = {"user_id": user_id, "messages": []}
    return {"conversation_id": conversation_id}

@app.post("/conversation/{conversation_id}/message/")
async def post_message(conversation_id: str, message: Message):
    if conversation_id not in app.conversations_db or app.conversations_db[conversation_id]['user_id'] != message.user_id:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    app.conversations_db[conversation_id]["messages"].append(message.message)
    return {"message": "Message posted successfully."}

@app.get("/conversations/{user_id}")
async def get_conversations(user_id: str) -> Dict[str, List[Dict[str, List[str]]]]:
    user_conversations = [convo for convo in app.conversations_db.values() if convo['user_id'] == user_id]
    return {"conversations": user_conversations}

@app.get("/conversation/{conversation_id}/messages/")
async def get_messages(conversation_id: str) -> Dict[str, List[str]]:
    if conversation_id not in app.conversations_db:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    return {"messages": app.conversations_db[conversation_id]["messages"]}

@app.get("/conversations/{user_id}/headers")
async def get_conversation_headers(user_id: str) -> Dict[str, List[str]]:
    user_conversation_ids = [convo_id for convo_id, convo in app.conversations_db.items() if convo['user_id'] == user_id]
    return {"conversations": user_conversation_ids}

# ====== Image Upload and Question Answering Endpoints ======
@app.post("/upload/{user_id}")
async def image_upload(user_id: str, image: Optional[UploadFile] = File(None), image_url: Optional[str] = None):
    print(f"Received in image_upload -> Image: {image}, Image URL: {image_url}")  # Debug Print
    try:
        if image:
            file_content = await image.read()
            await image.seek(0)
            try:
                Image.open(io.BytesIO(file_content))
            except IOError:
                raise HTTPException(status_code=400, detail="Invalid file type. Please upload an image file.")
            if len(file_content) > 30e6:
                raise HTTPException(status_code=400, detail="Image file size is too large. Please upload a smaller image.")
            image_url = await quickstart.upload_image_to_blob(image)
        elif not image_url:
            raise HTTPException(status_code=400, detail="Please provide an image or a valid image URL.")
        
        await app.redis.set(f"{conversation_id}-image_url", image_url)
        response = await quickstart.create_upload_file(image)
        situation = response["situation"]
        history = json.dumps(response["history"])

        session_id = str(uuid.uuid4())
        await app.redis.set(f"{conversation_id}-session_id", session_id)
        await app.redis.set(f"{session_id}-situation", situation)
        await app.redis.set(f"{session_id}-history", history)

        question_index = (await app.redis.get(f"{session_id}-question_index")) if (await app.redis.get(f"{session_id}-question_index")) is not None else "0"
        question_index = int(question_index.decode("utf-8")) if question_index != "0" else 0
        if question_index < len(preset_questions):
            question_to_ask = preset_questions[question_index]
            await app.redis.set(f"{session_id}-question_index", str(question_index + 1))
            await app.redis.set(f"{session_id}-question", question_to_ask)
            pickup_line_convo = PickupLineConversation(
                conversation_id=conversation_id,
                user_id=user_id,
                image_url=image_url,
                questions=[question_to_ask],
                answers=[]
            )
            app.pickup_line_conversations_db[conversation_id] = pickup_line_convo
            return {"message": "Upload successful.", "question_id": question_index, "question": question_to_ask, "conversation_id": conversation_id}

        return {"message": "Upload successful."}
    except HTTPException as e:
        logging.error(f"HTTP Exception occurred: {e.detail}")
        raise
    except Exception as e:
        logging.error(f"Unexpected error occurred: {str(e)}")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/answer/{conversation_id}/{question_id}")
async def answer_question(conversation_id: str, question_id: int, answer: str):
    session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
    if session_id_raw is None:
        raise HTTPException(status_code=404, detail="No active session found for this conversation.")
    session_id = session_id_raw.decode("utf-8")
    question_index_raw = await app.redis.get(f"{session_id}-question_index")
    if question_index_raw is None:
        raise HTTPException(status_code=404, detail="No question found to answer.")
    question_index = int(question_index_raw.decode("utf-8"))
    if question_index != question_id + 1:
        raise HTTPException(status_code=404, detail="No question found to answer.")

    question = (await app.redis.get(f"{session_id}-question")).decode("utf-8")
    situation, history = await dai.process_question_answer(question, answer)
    await app.redis.set(f"{session_id}-situation", situation)
    await app.redis.set(f"{session_id}-history", json.dumps(history))

    question_index = int((await app.redis.get(f"{session_id}-question_index")).decode("utf-8"))
    question_to_ask = None
    more_questions = False
    if question_index < len(preset_questions):
        question_to_ask = preset_questions[question_index]
        await app.redis.set(f"{session_id}-question_index", str(question_index + 1))
        await app.redis.set(f"{session_id}-question", question_to_ask)
        app.pickup_line_conversations_db[conversation_id].questions.append(question_to_ask)
        more_questions = True

    # Store the answer in Redis
    await app.redis.lpush(f"{session_id}-answers", answer)
   
    # Store the answer in the pickup line conversations database
    app.pickup_line_conversations_db[conversation_id].answers.append(answer)

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

@app.delete("/conversation/{user_id}/{conversation_id}")
async def delete_conversation(user_id: str, conversation_id: str):
    # Check if the conversation exists in either database
    if conversation_id not in app.conversations_db and conversation_id not in app.pickup_line_conversations_db:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    # If the conversation exists in conversations_db, check if the user_id matches
    if conversation_id in app.conversations_db:
        if app.conversations_db[conversation_id]['user_id'] != user_id:
            raise HTTPException(status_code=403, detail="User does not have permission to delete this conversation.")
        del app.conversations_db[conversation_id]

    # If the conversation exists in pickup_line_conversations_db, check if the user_id matches
    if conversation_id in app.pickup_line_conversations_db:
        if app.pickup_line_conversations_db[conversation_id].user_id != user_id:
            raise HTTPException(status_code=403, detail="User does not have permission to delete this conversation.")
        del app.pickup_line_conversations_db[conversation_id]

    return {"message": "Conversation deleted successfully."}

# Additional utility functions and routes can be added below

# For example, if you'd like to add an endpoint to fetch a specific message from a conversation, you can implement it like this:

@app.get("/conversation/{conversation_id}/message/{message_id}")
async def get_specific_message(conversation_id: str, message_id: int):
    if conversation_id not in app.conversations_db:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    try:
        message = app.conversations_db[conversation_id]["messages"][message_id]
        return {"message": message}
    except IndexError:
        raise HTTPException(status_code=404, detail="Message not found in the specified conversation.")

# Similarly, you can add more routes or utility functions as per your application's requirements.

# Endpoint to update a specific message in a conversation
@app.patch("/conversation/{conversation_id}/message/{message_id}")
async def update_specific_message(conversation_id: str, message_id: int, updated_message: str):
    if conversation_id not in app.conversations_db:
        raise HTTPException(status_code=404, detail="Conversation not found.")
    try:
        app.conversations_db[conversation_id]["messages"][message_id] = updated_message
        return {"message": "Message updated successfully."}
    except IndexError:
        raise HTTPException(status_code=404, detail="Message not found in the specified conversation.")

# Endpoint to retrieve all messages from a specific user
@app.get("/messages/{user_id}")
async def get_messages_by_user(user_id: str):
    messages = []
    for convo in app.conversations_db.values():
        if convo['user_id'] == user_id:
            messages.extend(convo["messages"])
    return {"messages": messages}

# Endpoint to delete all conversations of a user
@app.delete("/conversations/{user_id}")
async def delete_all_conversations_of_user(user_id: str):
    convo_ids_to_delete = [convo_id for convo_id, convo in app.conversations_db.items() if convo['user_id'] == user_id]
    for convo_id in convo_ids_to_delete:
        del app.conversations_db[convo_id]

    return {"message": "All conversations for the specified user have been deleted."}

# Additional endpoints or utility functions can be added below as per your application's requirements.

# ====== Twilio Endpoint ======

# ====== Twilio Endpoint ======

# Twilio Webhook Endpoint
@app.post("/twilio-webhook/")
async def twilio_webhook(request: Request):
    form_data = await request.form()
    incoming_msg = form_data.get('Body').strip()
    from_number = form_data.get('From').strip()
    user_id = from_number

    resp = MessagingResponse()
    msg = resp.message()

    user_state = await get_or_init_user_state(user_id)

    if user_state == "START":
        msg.body("Welcome to Wingman AI! Please upload an image to get started.")
        await set_user_state(user_id, "AWAITING_IMAGE")
    elif user_state == "AWAITING_IMAGE":
        if "MediaUrl0" in form_data:
            image_url = form_data.get('MediaUrl0')
            print(f"Extracted Image URL from Twilio in twilio_webhook: {image_url}")  # Debug Print
            response = await image_upload(user_id=user_id, image_url=image_url)
            response_data = json.loads(response.body.decode("utf-8"))
            question_text = response_data.get('question', "No question available.")
            msg.body(f"Upload successful! {question_text}")
            await set_user_state(user_id, f"QUESTION_{response_data['question_id']}")
        else:
            msg.body("Please upload an image to proceed.")
    elif user_state.startswith("QUESTION_"):
        question_id = int(user_state.split("_")[1])
        response = await answer_question(conversation_id=user_id, question_id=question_id, answer=incoming_msg)
        if response['more_questions']:
            msg.body(response['next_question'])
            await set_user_state(user_id, f"QUESTION_{question_id + 1}")
        else:
            pickup_lines = await generate_statements(conversation_id=user_id)
            msg.body(f"Based on your responses, here are some pickup lines: {', '.join(pickup_lines['pickup_line'])}. What would you like to do next?")
            await set_user_state(user_id, "AWAITING_COMMAND")
    elif user_state == "AWAITING_COMMAND":
        response = await process_command(conversation_id=user_id, command=incoming_msg)
        msg.body(response['assistant_response'])
        await set_user_state(user_id, "AWAITING_NEXT_ACTION")
    elif user_state == "AWAITING_NEXT_ACTION":
        msg.body("Thank you for using Wingman AI! If you'd like to start over, send 'restart'.")
    else:
        msg.body("Sorry, I couldn't understand that. Please try again.")
        await set_user_state(user_id, "START")

    return str(resp)
