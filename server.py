import logging
from PIL import Image
import io
from typing import List
from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel
import dai
import quickstart

# Database related imports
# import pyodbc

# Azure SQL DB connection string
# SERVER = 'your_server.database.windows.net'
# DATABASE = 'your_database'
# USERNAME = 'your_username'
# PASSWORD = 'your_password'
# DRIVER = '{ODBC Driver 17 for SQL Server}'

# Class definitions for handling database operations
# class Database:
#     def __init__(self):
#         self.conn = pyodbc.connect(f'DRIVER={DRIVER};SERVER={SERVER};DATABASE={DATABASE};UID={USERNAME};PWD={PASSWORD}')
#         self.cursor = self.conn.cursor()

#     def execute(self, statement):
#         self.cursor.execute(statement)
#         self.conn.commit()

#     def fetchone(self, statement):
#         self.cursor.execute(statement)
#         return self.cursor.fetchone()

#     def fetchall(self, statement):
#         self.cursor.execute(statement)
#         return self.cursor.fetchall()

# db = Database()

class Message(BaseModel):
    user_id: str
    content: str

class Conversation(BaseModel):
    id: str
    user_id: str
    messages: List[Message]

app = FastAPI()
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

# Below are the new API endpoints related to conversations and messages. Database calls have been commented out.

@app.post("/conversations", response_model=Conversation)
async def create_conversation(user_id: str):
    # db.execute(f"INSERT INTO Conversations (user_id) VALUES ('{user_id}')")
    # conversation = db.fetchone(f"SELECT * FROM Conversations WHERE user_id='{user_id}'")
    # return Conversation(**conversation)
    pass

@app.get("/conversations/{conversation_id}", response_model=Conversation)
async def get_conversation(conversation_id: str):
    # conversation = db.fetchone(f"SELECT * FROM Conversations WHERE id='{conversation_id}'")
    # messages = db.fetchall(f"SELECT * FROM Messages WHERE conversation_id='{conversation_id}'")
    # conversation['messages'] = messages
    # return Conversation(**conversation)
    pass

@app.post("/conversations/{conversation_id}/messages", response_model=Message)
async def create_message(conversation_id: str, message: Message):
    # db.execute(f"INSERT INTO Messages (conversation_id, user_id, content) VALUES ('{conversation_id}', '{message.user_id}', '{message.content}')")
    # return message
    pass

@app.get("/conversations/{conversation_id}/messages", response_model=List[Message])
async def get_messages(conversation_id: str):
    # messages = db.fetchall(f"SELECT * FROM Messages WHERE conversation_id='{conversation_id}'")
    # return messages
    pass
