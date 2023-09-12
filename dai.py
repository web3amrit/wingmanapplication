import logging
logging.basicConfig(level=logging.DEBUG)
import io
import os
import uuid
import requests
from typing import List, Dict, Optional
import json
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
    "Where are you right now? How would you describe the mood of the place?",
    "Can you describe your own appearance and activity at the time you noticed her?",
    "What is she doing right now? Can you describe her body language?",
    "Can you estimate her age range?",
    "Is she alone or with a group?",
    "How would you describe her overall style based on her attire? Does anything specific stand out about her outfit?",
#   "What initially caught your attention about her?",
    "Have you noticed any specific non-verbal cues or signals from her, like eye contact, smiles, or gestures?",
    "Is there anything about her that you'd like to mention in the pickup line?",
    "Can you give any additional details about her?"
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
    if not session_id_raw:
        logger.error(f"No session_id found for conversation: {conversation_id}")
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
    await app.redis.set(f"{session_id}-conversation_complete", "true")
    
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
        
        # Fetch the current conversation history, pickup lines, questions, and answers
        history = app.pickup_line_conversations_db[conversation_id].messages.copy()
        pickup_lines = app.pickup_line_conversations_db[conversation_id].pickup_lines.copy()
        questions = app.pickup_line_conversations_db[conversation_id].questions
        answers = app.pickup_line_conversations_db[conversation_id].answers

        # Process the user's command using the retrieved data
        response, updated_history = await dai.process_user_query(command, history, pickup_lines, questions, answers)
        logger.debug(f"Updated history after processing command: {updated_history}")

        # Update the in-memory database's conversation history with the user's command and the assistant's response
        app.pickup_line_conversations_db[conversation_id].messages.extend([
            {"role": "user", "content": command},
            {"role": "assistant", "content": response}
        ])

        # Return the assistant's response
        return {"assistant_response": response}

    except Exception as e:
        logger.error(f"Error processing command: {str(e)}")
        return {"error": "Failed to process command."}

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
    session_id = None
    pickup_lines = []
    history = []
    logger.debug(f"Initial history: {history}")

    # Retrieve the conversation history from Redis for this session
    history_raw = await app.redis.lrange(f"{session_id}-history", 0, -1)
    history = [json.loads(item.decode("utf-8")) for item in history_raw]

    try:
        user_id = From
        user_message = Body.strip().lower()

        # Prioritize handling the user's specific commands
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
                await app.redis.delete(f"{session_id}-answers")
                logger.debug(f"Deleted all session related data for session_id {session_id} from Redis.")
                await app.redis.delete(f"{session_id}-conversation_complete")
                await app.redis.set(f"{session_id}-question_index", "1")

            # Start a new conversation for this user
            conversation_id = f"{user_id}-{uuid.uuid4()}"
            session_id = f"{conversation_id}-{uuid.uuid4()}"  # Generating a new session ID

            logger.info(f"Generated session_id: {session_id}")
            
            await app.redis.set(f"{conversation_id}-session_id", session_id)
            logger.debug(f"Stored session_id {session_id} for conversation_id {conversation_id} in Redis.")


            # Verification
            retrieved_session_id = await app.redis.get(f"{conversation_id}-session_id")
            if not retrieved_session_id or retrieved_session_id.decode("utf-8") != session_id:
                logger.error(f"Failed to store session_id: {session_id} for conversation_id: {conversation_id}")
                twilio_response.message("An error occurred. Please try again later.")
                return Response(content=str(twilio_response), media_type="application/xml")

            app.pickup_line_conversations_db[conversation_id] = PickupLineConversation(
                conversation_id=conversation_id,
                user_id=user_id,
                questions=[],
                answers=[]
            )
            await app.redis.set(f"{user_id}-conversation_id", conversation_id)
            twilio_response.message("Conversation started! Please send 'skip' to go straight to the questions.")
            return Response(content=str(twilio_response), media_type="application/xml")
        elif user_message == 'cmd:end_conversation':
            # End the current conversation and clear the data
            conversation_id_raw = await app.redis.get(f"{user_id}-conversation_id")
            if conversation_id_raw:
                conversation_id = conversation_id_raw.decode("utf-8")
                if conversation_id in app.pickup_line_conversations_db:
                    app.pickup_line_conversations_db[conversation_id].active = False
                session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
                if session_id_raw:
                    session_id = session_id_raw.decode("utf-8")
                    await app.redis.delete(f"{session_id}-questions")
                    await app.redis.delete(f"{session_id}-answers")
                await app.redis.delete(f"{session_id}-question_index")  # Delete the question index when ending the conversation
                twilio_response.message("Conversation ended. To start a new conversation, send 'join line-breathing'.")
                return Response(content=str(twilio_response), media_type="application/xml")
            logger.debug(f"After cmd:end_conversation check. Current history: {history}")
            
        # Check if there's an existing session ID for this user's conversation
        conversation_id_raw = await app.redis.get(f"{user_id}-conversation_id")
        if not conversation_id_raw:
            logger.error("No conversation_id available for user.")
            twilio_response.message("An error occurred. Please try again later.")
            return Response(content=str(twilio_response), media_type="application/xml")

        conversation_id = conversation_id_raw.decode("utf-8")
        session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
        if not session_id_raw:
            logger.error("No session_id available for the current conversation.")
            twilio_response.message("An error occurred. Please try again later.")
            return Response(content=str(twilio_response), media_type="application/xml")

        # This is right after retrieving the session ID
        session_id = session_id_raw.decode("utf-8")
        logger.info(f"Retrieved session_id: {session_id}")

        # Retrieve the conversation history from Redis for this session
        history_raw = await app.redis.lrange(f"{session_id}-history", 0, -1)
        history = [json.loads(item.decode("utf-8")) for item in history_raw]
        logger.debug(f"Retrieved history for session_id ({session_id}): {history}")
        if not history_raw:
            logger.warning(f"No history found in Redis for session_id ({session_id}).")
        # Retrieve questions and answers from Redis
        questions_raw = await app.redis.lrange(f"{session_id}-questions", 0, -1)
        answers_raw = await app.redis.lrange(f"{session_id}-answers", 0, -1)
        questions = [q.decode("utf-8") for q in questions_raw]
        answers = [a.decode("utf-8") for a in answers_raw]

        # Generate the situation from the questions and answers
        situation = "\n".join([f"{q}: {a}" for q, a in zip(questions, answers)])
        logger.debug(f"Generated situation: {situation}")

        # Get the current question index
        question_index_raw = await app.redis.get(f"{session_id}-question_index")
        if question_index_raw:
            question_index = int(question_index_raw.decode("utf-8"))
            logger.info(f"Current question index for session {session_id}: {question_index}.")

            if question_index == -1:
                # If the question_index is -1, directly process the user's command with OpenAI
                try:
                    response, history = await dai.process_user_query(user_message, history, pickup_lines, questions, answers)
                    logger.debug(f"Updated history after processing user command: {history}")
                    response += "\n\nTo end the conversation, type 'cmd:end_conversation'"
                    twilio_response.message(response)
                    return Response(content=str(twilio_response), media_type="application/xml")
                except Exception as e:
                    logger.error(f"Error processing user command: {str(e)}")
                    twilio_response.message("Sorry, there was an error processing your request. Please try again.")
                    return Response(content=str(twilio_response), media_type="application/xml")

            # Only add to answers if it's not a command and the question index is valid
            if not user_message.startswith('cmd:') and question_index <= len(preset_questions):
                # Save the user's answer
                q_and_a = preset_questions[question_index - 1] + ": " + user_message
                await app.redis.lpush(f"{session_id}-answers", q_and_a)
                app.pickup_line_conversations_db[conversation_id].messages.append({"role": "user", "content": preset_questions[question_index - 1]})
                app.pickup_line_conversations_db[conversation_id].messages.append({"role": "assistant", "content": user_message})

                # Check if there are more questions to ask
                if question_index < len(preset_questions):
                    next_question = preset_questions[question_index]
                    await app.redis.set(f"{session_id}-question_index", str(question_index + 1))
                    twilio_response.message(next_question)
                    return Response(content=str(twilio_response), media_type="application/xml")
                # If all questions have been answered, generate pickup lines
                if question_index == len(preset_questions):
                    # Extract the user's answers
                    answers_raw = await app.redis.lrange(f"{session_id}-answers", 0, -1)
                    situation = "\n".join([answer.decode("utf-8") for answer in answers_raw])

                    # Generate the pickup lines based on the user's answers
                    pickup_lines, history = await generate_pickup_lines(situation, history, answers, num_lines=5)
                    for pl in pickup_lines:
                        app.pickup_line_conversations_db[conversation_id].messages.append({"role": "assistant", "content": pl})
                        await app.redis.lpush(f"{session_id}-history", *[json.dumps(item) for item in app.pickup_line_conversations_db[conversation_id].messages])

                    # Append questions, answers, and pickup lines to the history, ONLY ONCE:
                    for q, a in zip(questions, answers):
                        history.append({"role": "user", "content": q})
                        history.append({"role": "assistant", "content": a})
                    logger.debug(f"History after appending questions and answers: {history}")
                    for pl in pickup_lines:
                        history.append({"role": "assistant", "content": pl})
                    logger.debug(f"Appended pickup lines to history. Updated history: {history}")


                    # Save the situation and the pickup lines to Azure Blob Storage
                    conversation_content = situation + "\n\n" + "\n".join(pickup_lines)
                    save_conversation_to_blob(user_id, conversation_content)

                    # Join the pickup lines into a single message
                    message = "Here are your pickup lines:\n\n" + "\n\n".join(pickup_lines) + "\n\nNeed advice or tips? Input your request. To end the conversation, type 'cmd:end_conversation'"
                    twilio_response.message(message)
                    await app.redis.set(f"{session_id}-question_index", "-1")  # Indicate all questions have been asked
                    await app.redis.delete(f"{session_id}-answers")  # Reset the answers list
                    return Response(content=str(twilio_response), media_type="application/xml")
                
                logger.debug(f"Fetched question index: {question_index}")

            else:
                logger.error(f"No question index found for session {session_id}.")
        else:
            logger.error(f"No session found for conversation {conversation_id}.")

        # Check if the user has completed the conversation
        conversation_complete_raw = await app.redis.get(f"{session_id}-conversation_complete")
        if conversation_complete_raw and conversation_complete_raw.decode("utf-8") == "true":
            twilio_response.message("Your conversation is already marked as complete. To start a new conversation, send 'cmd:start_conversation'.")
            return Response(content=str(twilio_response), media_type="application/xml")
        # Handle "skip" input
        if user_message == "skip":
            conversation_id_raw = await app.redis.get(f"{user_id}-conversation_id")
            if not conversation_id_raw:
                logger.error(f"No conversation_id found for user {user_id} on skip command.")
                twilio_response.message("An error occurred. Please start a new conversation with 'cmd:start_conversation'.")
                return Response(content=str(twilio_response), media_type="application/xml")

            conversation_id = conversation_id_raw.decode("utf-8")
            session_id_raw = await app.redis.get(f"{conversation_id}-session_id")
            if not session_id_raw:
                logger.error(f"No session_id found for conversation_id: {conversation_id} on skip command.")
                twilio_response.message("An error occurred. Please start a new conversation with 'cmd:start_conversation'.")
                return Response(content=str(twilio_response), media_type="application/xml")

            session_id = session_id_raw.decode("utf-8")
            question_index_raw = await app.redis.get(f"{session_id}-question_index")
            if question_index_raw:
                question_index = int(question_index_raw.decode("utf-8"))
                if question_index < len(preset_questions):
                    next_question = preset_questions[question_index]
                    await app.redis.set(f"{session_id}-question_index", str(question_index + 1))
                    twilio_response.message(next_question)
                    return Response(content=str(twilio_response), media_type="application/xml")
                else:
                    logger.error(f"Question index out of range for session {session_id}.")
            else:
                await app.redis.set(f"{session_id}-question_index", "1")
                twilio_response.message(preset_questions[0])
                return Response(content=str(twilio_response), media_type="application/xml")
            logger.debug(f"After checking for skip. Current history: {history}")

        questions_raw = await app.redis.lrange(f"{session_id}-questions", 0, -1)
        answers_raw = await app.redis.lrange(f"{session_id}-answers", 0, -1)
        questions = [q.decode("utf-8") for q in questions_raw]
        answers = [a.decode("utf-8") for a in answers_raw]

        logger.debug(f"Retrieved questions: {questions}")
        logger.debug(f"Retrieved answers: {answers}")

        # After delivering pickup lines, if the user provides another input, it's likely a command for further advice or tips
        logger.debug(f"History before processing command: {history}")
        if conversation_id_raw:
            # Fetch the conversation history and pickup lines
            conversation_id = conversation_id_raw.decode("utf-8")

            # Start with the existing history if available
            if conversation_id in app.pickup_line_conversations_db:
                history = app.pickup_line_conversations_db[conversation_id].messages.copy()
                logger.debug(f"Fetched history for conversation_id {conversation_id}: {history}")
            else:
                history = []  # Ensure history is always initialized

            if question_index == -1:
                # Process the user's command
                try:
                    response, history = await dai.process_user_query(user_message, history, pickup_lines, questions, answers)
                    logger.debug(f"Updated history after processing user command: {history}")
                    response += "\n\nTo end the conversation, type 'cmd:end_conversation'"
                    twilio_response.message(response)
                    return Response(content=str(twilio_response), media_type="application/xml")
                except Exception as e:
                    logger.error(f"Error processing user command: {str(e)}")
                    twilio_response.message("Sorry, there was an error processing your request. Please try again.")
                    return Response(content=str(twilio_response), media_type="application/xml")
            logger.debug(f"In block for question_index == -1. Current history: {history}")

        # Check other general commands or handle the default flow
        else:
            try:
                response, history = await dai.process_user_query(user_message, history, pickup_lines, questions, answers)
                logger.debug(f"Updated history after processing user command: {history}")
                response += "\n\nTo end the conversation, type 'cmd:end_conversation'"
                twilio_response.message(response)
                return Response(content=str(twilio_response), media_type="application/xml")
            except Exception as e:
                logger.error(f"Error processing user command: {str(e)}")
                twilio_response.message("Sorry, there was an error processing your request. Please try again.")
                return Response(content=str(twilio_response), media_type="application/xml")

    except Exception as e:
        logger.error(f"Unhandled exception: {str(e)}")
        await app.redis.lpush(f"{session_id}-history", *[json.dumps(item) for item in history])  # Moved inside the async function
        twilio_response.message("Welcome to Wingman AI! To begin, send 'cmd:start_conversation'.")
        return Response(content=str(twilio_response), media_type="application/xml")

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
