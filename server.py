import os
import logging
from PIL import Image
import io
from typing import List
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import aioodbc
from fastapi_cache.decorator import cache
from fastapi_cache.coder import JsonCoder
import asyncio
import dai
import quickstart
import pyodbc
from aiocache import cached, Cache
from aiocache.serializers import PickleSerializer

# Azure SQL DB connection string
SERVER = os.environ['SERVER']
DATABASE = os.environ['DATABASE']
USERNAME = os.environ['USER_NAME']
PASSWORD = os.environ['PASSWORD']
DRIVER = os.environ['DRIVER']

# Class definitions for handling database operations
class Message(BaseModel):
    user_id: str
    content: str

class Conversation(BaseModel):
    id: str
    user_id: str
    messages: List[Message] = []

app = FastAPI(debug=True)

logging.basicConfig(level=logging.INFO)

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

# Initialize global variables for situation and history
situation_global = None
history_global = None

# Setup DSN (Data Source Name) for aioodbc
dsn = f'DRIVER={DRIVER};SERVER={SERVER};DATABASE={DATABASE};UID={USERNAME};PWD={PASSWORD}'

# Define the async functions for database operations
async def open_connection():
    try:
        conn = await aioodbc.connect(dsn)
        return conn
    except Exception as e:
        logging.error(f"Error opening connection: {e}")
        return None

async def close_connection(conn):
    if conn:
        try:
            await conn.close()
        except Exception as e:
            logging.error(f"Error closing connection: {e}")

async def execute_query(query, *params):
    conn = await open_connection()
    if conn is None:
        return None
    try:
        cur = await conn.cursor()
        await cur.execute(query, params)
        await conn.commit()
        return cur
    except Exception as e:
        logging.error(f"Error executing query: {e}")
        return None
    finally:
        await close_connection(conn)

# Caching configuration
cache = Cache(Cache.MEMORY, serializer=PickleSerializer())

@app.get("/")
async def root():
    return {"message": "Welcome to Wingman AI!"}

@app.post("/upload")
async def image_upload(image: UploadFile = File(...)):
    logging.info(f"Image details: Filename - {image.filename}, Content-Type - {image.content_type}")

    try:
        # File type validation
        file_content = await image.read()  # Read image file content
        await image.seek(0)  # Reset file pointer to start

        try:
            Image.open(io.BytesIO(file_content))  # Try to open the file content as an image
        except IOError:
            logging.error("Invalid file type.")
            raise HTTPException(
                status_code=400,
                detail="Invalid file type. Please upload an image file."
            )

        # Image size validation
        if len(file_content) > 1e6:  # Larger than 1 MB
            logging.error("Image file size is too large.")
            raise HTTPException(
                status_code=400,
                detail="Image file size is too large. Please upload a smaller image."
            )

        # Quickstart image file upload function call
        logging.info("Sending image to quickstart.create_upload_file.")
        global situation_global, history_global
        situation_global, history_global = await quickstart.create_upload_file(image)
        return {"message": "Upload and question asking successful."}

    except HTTPException as e:
        logging.exception("HTTP Exception occurred.")
        raise
    except Exception as e:
        logging.exception("Unexpected error occurred.")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/ask")
async def post_questions(query: str):
    try:
        if not query:
            logging.error("Query cannot be empty.")
            raise HTTPException(status_code=400, detail="Query cannot be empty.")

        logging.info("Processing user query.")
        response = await dai.process_user_query(query, [])
        return {"response": response}
    except Exception as e:
        logging.exception("Unexpected error occurred.")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/generate")
async def generate_statements(query: str):
    try:
        if not query:
            logging.error("Query cannot be empty.")
            raise HTTPException(status_code=400, detail="Query cannot be empty.")

        global situation_global, history_global
        if situation_global is None or history_global is None:
            return JSONResponse(status_code=400, content={"message": "Please upload an image first."})

        logging.info("Generating pickup lines.")
        pickup_lines = await dai.generate_pickup_lines(situation_global, history_global, 5)
        return {"pickup_lines": pickup_lines}
    except Exception as e:
        logging.exception("Unexpected error occurred.")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

# Below are the new API endpoints related to conversations and messages.

@app.post("/conversations", response_model=Conversation)
async def create_conversation(user_id: str):
    query = "INSERT INTO Conversations (user_id) VALUES (?);"
    cursor = await execute_query(query, user_id)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not create conversation.")
    conversation_id = cursor.lastrowid
    return Conversation(id=conversation_id, user_id=user_id)

@app.get("/conversations/{user_id}", response_model=List[Conversation])
@cached(ttl=10, cache=cache, key_builder=lambda self: f"conversations-{self.user_id}")
async def get_conversations(user_id: str):
    query = "SELECT * FROM Conversations WHERE user_id = ?;"
    cursor = await execute_query(query, user_id)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not retrieve conversations.")
    rows = await cursor.fetchall()
    return [Conversation(id=row.id, user_id=row.user_id) for row in rows]

cache = InMemoryCache(prefix="main", coder=JsonCoder())

@app.get("/conversations/{conversation_id}/messages", response_model=List[Message])
@cache(ttl=10, key_builder=lambda conversation_id: f"messages-{conversation_id}")
async def get_messages(conversation_id: str):
    query = "SELECT * FROM Messages WHERE conversation_id = ?;"
    cursor = await execute_query(query, conversation_id)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not retrieve messages.")
    rows = await cursor.fetchall()
    return [Message(user_id=row.user_id, content=row.content) for row in rows]

@app.post("/conversations", response_model=Conversation)
async def create_conversation(user_id: str):
    query = "INSERT INTO Conversations (user_id) VALUES (?);"
    cursor = await execute_query(query, user_id)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not create conversation.")
    conversation_id = cursor.lastrowid
    return Conversation(id=conversation_id, user_id=user_id)

@app.post("/conversations", response_model=Conversation)
async def create_conversation(user_id: str):
    query = "INSERT INTO Conversations (user_id) VALUES (?);"
    cursor = await execute_query(query, user_id)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not create conversation.")
    conversation_id = cursor.lastrowid
    return Conversation(id=conversation_id, user_id=user_id)

@app.get("/conversations/{user_id}", response_model=List[Conversation])
@cache()
async def get_conversations(user_id: str):
    query = "SELECT * FROM Conversations WHERE user_id = ?;"
    cursor = await execute_query(query, user_id)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not retrieve conversations.")
    rows = await cursor.fetchall()
    return [Conversation(id=row.id, user_id=row.user_id) for row in rows]

@app.get("/conversations/{conversation_id}/messages", response_model=List[Message])
@cache()
async def get_messages(conversation_id: str):
    query = "SELECT * FROM Messages WHERE conversation_id = ?;"
    cursor = await execute_query(query, conversation_id)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not retrieve messages.")
    rows = await cursor.fetchall()
    return [Message(user_id=row.user_id, content=row.content) for row in rows]

@app.post("/conversations/{conversation_id}/messages", response_model=Message)
async def create_message(conversation_id: str, message: Message):
    query = "INSERT INTO Messages (conversation_id, user_id, content) VALUES (?, ?, ?);"
    cursor = await execute_query(query, conversation_id, message.user_id, message.content)
    if cursor is None:
        raise HTTPException(status_code=400, detail="Could not create message.")
    message_id = cursor.lastrowid
    return Message(user_id=message.user_id, content=message.content)
