import logging
import io
import os
import uuid
import requests
from typing import List, Dict, Optional
import json
from azure.storage.blob import BlobServiceClient
from datetime import datetime


from fastapi import FastAPI, HTTPException, UploadFile, File, Request, Form
from dai import generate_pickup_lines
from azure.storage.blob import BlobServiceClient
from starlette.middleware.cors import CORSMiddleware
from starlette.responses import JSONResponse
from pydantic import BaseModel
from PIL import Image
from aioredis import Redis
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse


from starlette.responses import Response


import dai
import quickstart
import aioredis


logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)


app = FastAPI(debug=True)


# Initialize Azure Blob Storage Client
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


from typing import Optional, List, Dict
from pydantic import BaseModel


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
            print(f"Blob for user {self.user_id} does not exist. Creating...")
            # Ensure the blob is created as an AppendBlob
            self.blob_client.create_append_blob()
            print(f"Blob for user {self.user_id} created.")


    def log_message(self, role, message):
        # Since we have an AppendBlob, we can use append_block
        self.blob_client.append_block(f"{role}: {message}\n")


class PickupLineConversation(BaseModel):
    conversation_id: str
    user_id: str
    image_url: Optional[str] = None
    questions: List[str] = []
    answers: List[str] = []
    pickup_lines: List[str] = []
    messages: List[Dict[str, str]] = []
    active: bool = True  # [New] Track if the conversation is active or ended


app.conversations_db: Dict[str, Dict[str, List[str]]] = {}
app.pickup_line_conversations_db: Dict[str, PickupLineConversation] = {}


from typing import Optional


@app.on_event("startup")
async def startup_event():
    app.redis = await Redis.from_url(os.getenv('REDIS_CONNECTION_STRING'))


@app.on_event("shutdown")
async def shutdown_event():
    app.redis.close()


@app.get("/")
async def root():
    return {"message": "Welcome to Wingman AI!"}


@app.post("/conversation/")
async def create_conversation(user_id: str) -> Dict[str, str]:
    conversation_id = f"{user_id}-{uuid.uuid4()}"  # Combining user_id with UUID for uniqueness
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


from fastapi import Depends
from twilio.request_validator import RequestValidator
from twilio.twiml.messaging_response import MessagingResponse




@app.post("/upload/{user_id}")
async def image_upload(user_id: str, MediaUrl0: Optional[str] = None):
    conversation_id = str(uuid.uuid4())


    # Fetching the image from the URL provided by Twilio
    image_response = requests.get(MediaUrl0)
    image_content = image_response.content


    try:
        # Check if the content is a valid image
        try:
            Image.open(io.BytesIO(image_content))
        except IOError:
            raise HTTPException(
                status_code=400,
                detail="Invalid file type. Please upload an image file."
            )


        # Check the size of the image
        if len(image_content) > 30e6:
            raise HTTPException(
                status_code=400,
                detail="Image file size is too large. Please upload a smaller image."
            )


        image_url = await quickstart.upload_image_to_blob(image_content)  # Modified this function to handle content
        await app.redis.set(f"{conversation_id}-image_url", image_url)
        response = await quickstart.create_upload_file(image_content)  # Modified this function to handle content
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
   
from fastapi import Form
from twilio.twiml.messaging_response import MessagingResponse


@app.post("/answer/{conversation_id}/{question_id}")
async def answer_question(conversation_id: str, question_id: int, Body: str = Form(...)):
    if conversation_id not in app.pickup_line_conversations_db:
        logger.error(f"Conversation {conversation_id} not found in pickup_line_conversations_db.")
        raise HTTPException(status_code=404, detail="Conversation not found.")
   
    user_id = app.pickup_line_conversations_db[conversation_id].user_id
    conversation_id_raw = await app.redis.get(f"{user_id}-conversation_id")
    if conversation_id_raw is None:
        logger.error(f"No active conversation found for user {user_id}")
        raise HTTPException(status_code=404, detail="No active conversation found for this user.")
   
    session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
    if session_id_raw is None:
        logger.error(f"Session not found for conversation {conversation_id}.")
        raise HTTPException(status_code=404, detail="Session not found.")
   
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
    # Assuming `dai.process_question_answer()` is a function that you have defined
    situation, history = await dai.process_question_answer(question, Body)
    await app.redis.set(f"{session_id}-situation", situation)
    await app.redis.set(f"{session_id}-history", json.dumps(history))


    more_questions = False
    question_to_ask = None
    if question_index < len(preset_questions):
        question_to_ask = preset_questions[question_index]
        await app.redis.set(f"{session_id}-question_index", str(question_index + 1))
        await app.redis.set(f"{session_id}-question", question_to_ask)
        more_questions = True


    await app.redis.lpush(f"{session_id}-answers", Body)
    app.pickup_line_conversations_db[conversation_id].answers.append(Body)


    if more_questions:
        response_msg = f"Thanks for your answer! Next Question: {question_to_ask}"
    else:
        response_msg = "Thanks for your answer! We'll now generate your pickup lines."


    response = MessagingResponse()
    response.message(response_msg)
    return Response(content=str(response), media_type="application/xml")


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
   
    response = MessagingResponse()
    for line in pickup_lines:
        response.message(line)
    return Response(content=str(response), media_type="application/xml")


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


@app.post("/twilio-webhook")
async def twilio_webhook(Body: str = Form(...), From: str = Form(...), MediaUrl0: Optional[str] = Form(None)):
    twilio_response = MessagingResponse()
    user_id = From
    user_message = Body.strip().lower()


    # If user sends the cmd:start_conversation command
    if user_message == 'cmd:start_conversation':
        # Clear any existing state for this user in Redis
        session_id_raw = await app.redis.get(f"{user_id}-session_id")
        if session_id_raw:
            session_id = session_id_raw.decode("utf-8")
            await app.redis.delete(f"{user_id}-session_id")
            await app.redis.delete(f"{session_id}-situation")
            await app.redis.delete(f"{session_id}-history")
            await app.redis.delete(f"{session_id}-question_index")
            await app.redis.delete(f"{session_id}-question")


        # Start a new conversation for this user
        conversation_id = f"{user_id}-{uuid.uuid4()}"
        app.pickup_line_conversations_db[conversation_id] = PickupLineConversation(
            conversation_id=conversation_id,
            user_id=user_id,
            questions=[],
            answers=[]
        )
        await app.redis.set(f"{user_id}-conversation_id", conversation_id)
        twilio_response.message("Conversation started! Please upload an image or send 'skip' to go straight to the questions.")
        return Response(content=str(twilio_response), media_type="application/xml")


    # Check if the user has an active conversation
    conversation_id_raw = await app.redis.get(f"{user_id}-conversation_id")
    if conversation_id_raw:
        logger.info(f"Active conversation found for user {user_id}.")
        conversation_id = conversation_id_raw.decode("utf-8")
        # Get the session ID for the conversation
        session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
        if session_id_raw:
            session_id = session_id_raw.decode("utf-8")


            # Get the current question index
            question_index_raw = await app.redis.get(f"{session_id}-question_index")
            if question_index_raw:
                question_index = int(question_index_raw.decode("utf-8"))
               
                # Store the current answer
                await app.redis.lpush(f"{session_id}-answers", user_message)
               
                # Check if there are more questions to ask
                if question_index < len(preset_questions):
                    next_question = preset_questions[question_index]
                    await app.redis.set(f"{session_id}-question_index", str(question_index + 1))
                    twilio_response.message(next_question)
                    return Response(content=str(twilio_response), media_type="application/xml")
               
                # All questions have been answered
                else:
                    # Extract the user's situation and answers
                    answers_raw = await app.redis.lrange(f"{session_id}-answers", 0, -1)
                    answers = [answer.decode("utf-8") for answer in answers_raw]
                    situation = ""  # If you have a specific situation to initialize with, put it here
                   
                    # Generate the pickup lines based on the user's answers
                    pickup_lines = await generate_pickup_lines(situation, [], answers, num_lines=3)
                    conversation_content = "\n".join(pickup_lines)
                    save_conversation_to_blob(user_id, conversation_content)
                   
                    # Join the pickup lines into a single message
                    message = "Here are your pickup lines:\n\n" + "\n\n".join(pickup_lines)
                    twilio_response.message(message)
                    return Response(content=str(twilio_response), media_type="application/xml")
            else:
                logger.error(f"No question index found for session {session_id}.")
        else:
            logger.error(f"No session found for conversation {conversation_id}.")
    else:
        logger.info(f"No active conversation found for user {user_id}.")


    # If user sends specific commands
    if user_message == 'cmd:start_conversation':
        # Start a new conversation
        conversation_id = f"{user_id}-{uuid.uuid4()}"
        app.pickup_line_conversations_db[conversation_id] = PickupLineConversation(
            conversation_id=conversation_id,
            user_id=user_id,
            questions=[],
            answers=[]
        )
        await app.redis.set(f"{user_id}-conversation_id", conversation_id)
        twilio_response.message("Conversation started! Please upload an image or send 'skip' to go straight to the questions.")
        return Response(content=str(twilio_response), media_type="application/xml")


    elif user_message == 'skip':
        # Check if the user has an active conversation
        conversation_id_raw = await app.redis.get(f"{user_id}-conversation_id")
        if conversation_id_raw:
            conversation_id = conversation_id_raw.decode("utf-8")
        else:
            # If there's no active conversation, create a new one
            conversation_id = f"{user_id}-{uuid.uuid4()}"
            await app.redis.set(f"{user_id}-conversation_id", conversation_id)


        # Start the questions directly using the existing or new conversation_id
        first_question = await start_questions_directly(user_id, conversation_id)
        twilio_response.message(first_question)
        return Response(content=str(twilio_response), media_type="application/xml")




    elif user_message == 'cmd:end_conversation':
        # End the current conversation and clear the data
        conversation_id_raw = await app.redis.get(f"{user_id}-conversation_id")
        if conversation_id_raw:
            conversation_id = conversation_id_raw.decode("utf-8")
            if conversation_id in app.pickup_line_conversations_db:
                app.pickup_line_conversations_db[conversation_id].active = False
            await app.redis.delete(f"{user_id}-conversation_id")
        twilio_response.message("Conversation ended. Thank you!")
        return Response(content=str(twilio_response), media_type="application/xml")




    # Check if the user sent an image
    if MediaUrl0:
        response = await image_upload(user_id=user_id, MediaUrl0=MediaUrl0)
        twilio_response.message(response["message"])
        twilio_response.message(response["question"])
        return Response(content=str(twilio_response), media_type="application/xml")


    # If neither, assume the user is starting a new conversation
    twilio_response.message("Welcome to Wingman AI! Please upload an image to begin or send 'cmd:start_conversation' to initiate a conversation.")
    return Response(content=str(twilio_response), media_type="application/xml")




async def start_questions_directly(user_id: str, conversation_id: str) -> str:
    # Create a new session ID
    session_id = str(uuid.uuid4())
    await app.redis.set(f"{conversation_id}-session_id", session_id)
   
    # Start with the first question
    question_to_ask = preset_questions[0]
    await app.redis.set(f"{session_id}-question_index", "1")
    await app.redis.set(f"{session_id}-question", question_to_ask)
    await app.redis.set(f"{user_id}-session_id", session_id)
   
    # Store in the pickup_line_conversations_db
    pickup_line_convo = PickupLineConversation(
        conversation_id=conversation_id,
        user_id=user_id,
        questions=[question_to_ask],
        answers=[]
    )
    app.pickup_line_conversations_db[conversation_id] = pickup_line_convo
   
    return f"Let's start! First Question: {question_to_ask}"








def save_conversation_to_blob(phone_number, conversation_content):
    try:
        # Create a unique filename using the phone number and current timestamp
        current_timestamp = datetime.now().strftime("%Y%m%d%H%M%S")
        filename = f"{phone_number}_{current_timestamp}.txt"
       
        # Azure Blob Storage configurations
        connection_string = f"DefaultEndpointsProtocol=https;AccountName=wingmanblobstorage;AccountKey=eWcnrc2LVNrMLssTJ/laRqqqq+JaQXVi1HHGoSxD9tyMifH6D/IOoKjq3RL56XYg0WLnMKc4GQzh+AStnh+XWQ==;EndpointSuffix=core.windows.net"
       
        # Create the BlobServiceClient object which will be used to create a container client
        blob_service_client = BlobServiceClient.from_connection_string(connection_string)


        # Create a blob client using the generated filename for the blob
        blob_client = blob_service_client.get_blob_client(container="conversations", blob=filename)


        # Upload the file to blob
        blob_client.upload_blob(conversation_content, overwrite=True)
        print(f"Conversation under {phone_number} uploaded to Azure Blob Storage successfully!")


    except Exception as ex:
        print(f"Error in saving conversation to Azure Blob Storage: {str(ex)}")



