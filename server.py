import logging
import io
import os
import uuid
from typing import List, Dict, Optional
import json

from fastapi import FastAPI, HTTPException, UploadFile, File
from azure.storage.blob import BlobServiceClient
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
from aioredis import Redis

import dai
import quickstart
import aioredis

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(debug=True)

# Initialize Azure Blob Storage Client
connection_string = "DefaultEndpointsProtocol=https;AccountName=wingmanblobstorage;AccountKey=eWcnrc2LVNrMLssTJ/laRqqqq+JaQXVi1HHGoSxD9tyMifH6D/IOoKjq3RL56XYg0WLnMKc4GQzh+AStnh+XWQ==;EndpointSuffix=core.windows.net"
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
# Set up the logger
logging.basicConfig(level=logging.INFO, 
                    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
                    handlers=[logging.StreamHandler(), logging.FileHandler("server.log")])
logger = logging.getLogger(__name__)

class AzureConversationLogger:
    def __init__(self, user_id, blob_service_client, container_name):
        self.user_id = user_id
        self.blob_service_client = blob_service_client
        self.container_name = container_name
        self.blob_client = self.blob_service_client.get_blob_client(container=self.container_name, blob=f"{self.user_id}.txt")
        
        if not self.blob_client.exists():
            print(f"Blob for user {self.user_id} does not exist. Creating...")
            # Ensure the blob is created as an AppendBlob
            self.blob_client.create_append_blob()
            print(f"Blob for user {self.user_id} created.")


    def log_message(self, role, message):
        # Since we have an AppendBlob, we can use append_block
        self.blob_client.append_block(f"{role}: {message}\n")

    def save_to_blob(self):
        pass  # Since we're using append_block, we don't need a separate save function

class PickupLineConversation(BaseModel):
    conversation_id: str
    user_id: str
    image_url: Optional[str] = None
    questions: List[str] = []
    answers: List[str] = []
    pickup_lines: List[str] = []
    messages: List[Dict[str, str]] = []

app.conversations_db: Dict[str, Dict[str, List[str]]] = {}
app.pickup_line_conversations_db: Dict[str, PickupLineConversation] = {}

@app.on_event("startup")
async def startup_event():
    app.redis = await Redis.from_url(os.getenv('REDIS_CONNECTION_STRING'))

@app.on_event("shutdown")
async def shutdown_event():
    app.redis.close()
    await app.redis.wait_closed()

@app.get("/")
async def root():
    return {"message": "Welcome to Wingman AI!"}

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

    # Log the message to Azure Blob Storage
    logger = AzureConversationLogger(message.user_id, blob_service_client, container_name)
    logger.log_message("User", message.message)
    
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

@app.post("/upload/{user_id}")
async def image_upload(user_id: str, image: UploadFile = File(...)):
    conversation_id = str(uuid.uuid4())
    try:
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
        logger.error(f"No active session found for conversation {conversation_id}")
        raise HTTPException(status_code=404, detail="No active session found for this conversation.")
    session_id = session_id_raw.decode("utf-8")
    question_index_raw = await app.redis.get(f"{session_id}-question_index")
    if question_index_raw is None:
        logger.error(f"No question found for session {session_id}")
        raise HTTPException(status_code=404, detail="No question found to answer.")
    question_index = int(question_index_raw.decode("utf-8"))
    if question_index != question_id + 1:
        logger.error(f"Question index mismatch for session {session_id}")
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

    # Log the user's answer and next question to Azure Blob Storage
    logger = AzureConversationLogger(app.pickup_line_conversations_db[conversation_id].user_id, blob_service_client, container_name)
    logger.log_message("User", answer)
    if question_to_ask:
        logger.log_message("Assistant", f"Next Question: {question_to_ask}")
    
    return {"message": "Answer processed successfully.", "more_questions": more_questions, "next_question": question_to_ask}

@app.post("/generate/{conversation_id}")
async def generate_statements(conversation_id: str):
    session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
    if session_id_raw is None:
        logger.error(f"No active session found for conversation {conversation_id}")
        raise HTTPException(status_code=404, detail="No active session found for this conversation.")
    session_id = session_id_raw.decode("utf-8")

    situation = (await app.redis.get(f"{session_id}-situation")).decode("utf-8")
    history = json.loads((await app.redis.get(f"{session_id}-history")).decode("utf-8"))
    
    # Retrieve answers
    answers = app.pickup_line_conversations_db[conversation_id].answers

    # Pass answers to the function that generates pickup lines
    pickup_lines = await dai.generate_pickup_lines(situation, history, answers, 5)

    app.pickup_line_conversations_db[conversation_id].pickup_lines = pickup_lines

    # Log the generated pickup lines to Azure Blob Storage
    logger = AzureConversationLogger(app.pickup_line_conversations_db[conversation_id].user_id, blob_service_client, container_name)
    for line in pickup_lines:
        logger.log_message("Assistant", line)
    
    return {"pickup_line": pickup_lines}

@app.post("/process-command/{conversation_id}/{command}")
async def process_command(conversation_id: str, command: str) -> Dict[str, str]:
    try:
        # Check if the conversation_id exists in the pickup_line_conversations_db
        if conversation_id not in app.pickup_line_conversations_db:
            logger.error(f"Conversation {conversation_id} not found.")
            raise HTTPException(status_code=404, detail="Conversation not found.")
        
        # Check if the command is properly formed
        if not command:
            logger.error(f"Invalid command for conversation {conversation_id}")
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

        # Log the user's command to Azure Blob Storage
        logger = AzureConversationLogger(app.pickup_line_conversations_db[conversation_id].user_id, blob_service_client, container_name)
        logger.log_message("User", command)
        
        return {"assistant_response": response}

    except HTTPException as e:
        logger.error(f"HTTP Exception for conversation {conversation_id}: {e.detail}")
        return JSONResponse(status_code=e.status_code, content={"message": e.detail})
    except Exception as e:
        logging.error(f"Unexpected error for conversation {conversation_id}: {str(e)}")
        return JSONResponse(status_code=500, content={"message": "An unexpected error occurred."})

@app.delete("/conversation/{user_id}/{conversation_id}")
async def delete_conversation(user_id: str, conversation_id: str):
    # Check if the conversation exists in either database
    if conversation_id not in app.conversations_db and conversation_id not in app.pickup_line_conversations_db:
        logger.error(f"Conversation {conversation_id} for user {user_id} not found.")
        raise HTTPException(status_code=404, detail="Conversation not found.")

    # If the conversation exists in conversations_db, check if the user_id matches
    if conversation_id in app.conversations_db:
        if app.conversations_db[conversation_id]['user_id'] != user_id:
            logger.error(f"User {user_id} does not have permission to delete conversation {conversation_id}.")
            raise HTTPException(status_code=403, detail="User does not have permission to delete this conversation.")
        del app.conversations_db[conversation_id]

    # If the conversation exists in pickup_line_conversations_db, check if the user_id matches
    if conversation_id in app.pickup_line_conversations_db:
        if app.pickup_line_conversations_db[conversation_id].user_id != user_id:
            logger.error(f"User {user_id} does not have permission to delete conversation {conversation_id}.")
            raise HTTPException(status_code=403, detail="User does not have permission to delete this conversation.")
        del app.pickup_line_conversations_db[conversation_id]

    logging.info(f"Deleted conversation {conversation_id} for user {user_id}.")
    return {"message": "Conversation deleted successfully."}
