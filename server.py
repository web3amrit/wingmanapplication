import logging
import io
import uuid
from typing import List, Dict, Optional

from fastapi import FastAPI, HTTPException, UploadFile, File
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image

import dai
import quickstart

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(debug=True)
app.add_middleware(SessionMiddleware, secret_key="test")
session_data = {}

allowed_origins = [
    "http://localhost:8000",
    "http://localhost",
    "http://localhost:8080",
    "http://127.0.0.1:8000"
]

app.add_middleware(
    CORSMiddleware,
    allow_origins=allowed_origins,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

preset_questions = ["What is her age range", "What is she wearing", "What are her actions"]

class Message(BaseModel):
    user_id: str
    message: str

class Conversation(BaseModel):
    user_id: str
    conversation_id: Optional[str] = None
    messages: List[str] = []

class PickupLineConversation(BaseModel):
    conversation_id: str
    user_id: str
    questions: List[str] = []
    pickup_lines: List[str] = []

app.conversations_db: Dict[str, Dict[str, List[str]]] = {}
app.pickup_line_conversations_db: Dict[str, PickupLineConversation] = {}

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

        if len(file_content) > 1e6:
            raise HTTPException(
                status_code=400,
                detail="Image file size is too large. Please upload a smaller image."
            )

        response = await quickstart.create_upload_file(image)
        situation = response["situation"]
        history = response["history"]

        # Start asking questions right after the image upload.
        session_id = str(uuid.uuid4())  # Create a new session ID.
        session_data[f"{session_id}-situation"] = situation
        session_data[f"{session_id}-history"] = history

        conversation_id = str(uuid.uuid4())  # Create a new conversation ID.
        question_index = session_data.get(f"{session_id}-question_index", 0)
        if question_index < len(preset_questions):
            question_to_ask = preset_questions[question_index]
            session_data[f"{session_id}-question_index"] = question_index + 1
            session_data[f"{session_id}-question"] = question_to_ask
            app.pickup_line_conversations_db[conversation_id] = PickupLineConversation(
                conversation_id=conversation_id, user_id=user_id, questions=[question_to_ask]
            )
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
    session_id = session_data.get(f"{conversation_id}-session_id")
    if not session_id or session_data.get(f"{session_id}-question_index", 0) != question_id + 1:
        raise HTTPException(status_code=404, detail="No question found to answer.")

    question = session_data.get(f"{session_id}-question")
    situation, history = await dai.process_question_answer(question, answer)
    session_data[f"{session_id}-situation"] = situation
    session_data[f"{session_id}-history"] = history

    # Generate the next question
    question_index = session_data.get(f"{session_id}-question_index", 0)
    if question_index < len(preset_questions):
        question_to_ask = preset_questions[question_index]
        session_data[f"{session_id}-question_index"] = question_index + 1
        session_data[f"{session_id}-question"] = question_to_ask
        app.pickup_line_conversations_db[conversation_id].questions.append(question_to_ask)

    return {"message": "Answer processed successfully.", "more_questions": question_index < len(preset_questions)}

@app.post("/generate/{conversation_id}")
async def generate_statements(conversation_id: str):
    session_id = session_data.get(f"{conversation_id}-session_id")
    if not session_id:
        raise HTTPException(status_code=404, detail="No active session found for this conversation.")

    situation = session_data.get(f"{session_id}-situation")
    history = session_data.get(f"{session_id}-history")

    pickup_lines = await dai.generate_pickup_lines(situation, history, 5)  # Added 'await' here.

    app.pickup_line_conversations_db[conversation_id].pickup_lines = pickup_lines

    return {"pickup_line": pickup_lines}

@app.get("/pickup-line-conversations/{user_id}")
async def get_pickup_line_conversations(user_id: str):
    return {"pickup_line_conversations": [convo.dict() for convo in app.pickup_line_conversations_db.values() if convo.user_id == user_id]}

@app.get("/pickup-line-conversation/{conversation_id}")
async def get_pickup_line_conversation(conversation_id: str):
    if conversation_id not in app.pickup_line_conversations_db:
        raise HTTPException(status_code=404, detail="Pickup line conversation not found.")
    return {"pickup_line_conversation": app.pickup_line_conversations_db[conversation_id].dict()}
