import logging
import io
import uuid
from typing import List, Dict, Optional

from fastapi import FastAPI, Depends, HTTPException, UploadFile, File
from starlette.middleware.cors import CORSMiddleware
from starlette.middleware.sessions import SessionMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel

import dai
import quickstart

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

app = FastAPI(debug=True)
app.add_middleware(SessionMiddleware, secret_key="test")

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

preset_questions = ["Question 1", "Question 2", "Question 3"]

app.conversations_db: Dict[str, Dict[str, List[str]]] = {}

class Message(BaseModel):
    user_id: str
    message: str

class Conversation(BaseModel):
    user_id: str
    conversation_id: Optional[str] = None
    messages: List[str] = []

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

@app.post("/upload")
async def image_upload(image: UploadFile = File(...)):
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

        situation, history = await quickstart.create_upload_file(image)

        return {"message": "Upload successful."}
    except HTTPException as e:
        logging.error(f"HTTP Exception occurred: {e.detail}")
        raise
    except Exception as e:
        logging.error(f"Unexpected error occurred: {str(e)}")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/ask")
async def ask_question(session_id: str):
    try:
        question_index = session_data.get(f"{session_id}-question_index", 0)

        if question_index >= len(preset_questions):
            return {"message": "All questions asked"}

        question_to_ask = preset_questions[question_index]

        session_data[f"{session_id}-question_index"] = question_index + 1

        return {"question": question_to_ask}
    except Exception as e:
        logging.error(f"Unexpected error occurred: {str(e)}")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/answer")
async def post_answer(answer: str, session_id: str):
    try:
        if not answer:
            raise HTTPException(status_code=400, detail="Answer cannot be empty.")

        situation = session_data.get(f"{session_id}-situation")
        history = session_data.get(f"{session_id}-history")

        new_situation, new_history = await dai.process_question_answer(preset_questions[session_data.get(f"{session_id}-question_index") - 1], answer)

        session_data[f"{session_id}-situation"] = new_situation
        session_data[f"{session_id}-history"] = new_history

        return {"message": "Answer received and processed"}
    except HTTPException as e:
        logging.error(f"HTTP Exception occurred: {e.detail}")
        raise
    except Exception as e:
        logging.error(f"Unexpected error occurred: {str(e)}")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/generate")
async def generate_statements(session_id: str):
    try:
        situation = session_data.get(f"{session_id}-situation")
        history = session_data.get(f"{session_id}-history")

        # Implement your function logic here. You may need to replace the following lines.
        # Let's just assume that it should return situation and history for now.
        return {"situation": situation, "history": history}
    except Exception as e:
        logging.error(f"Unexpected error occurred: {str(e)}")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})
