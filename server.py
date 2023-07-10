import logging
from PIL import Image
import io
from fastapi import FastAPI, HTTPException, UploadFile, File, Depends
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi_cache.cachefast import CacheFast
from fastapi_cache.backends.memory import CACHE_BACKEND
from fastapi_cache.decorator import cache
from starlette.middleware.sessions import SessionMiddleware
import dai
import quickstart

app = FastAPI(debug=True)

# Add session middleware
app.add_middleware(SessionMiddleware, secret_key="YOUR-SECRET-KEY")

# Initialize cache
cache_fast = CacheFast(CACHE_BACKEND)
app.state.cache = cache_fast

logging.basicConfig(level=logging.INFO)  # Change to DEBUG for more detailed log

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
        situation, history = await quickstart.create_upload_file(image)

        return {"message": "Upload successful."}

    except HTTPException as e:
        logging.exception("HTTP Exception occurred.")
        raise
    except Exception as e:
        logging.exception("Unexpected error occurred.")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/ask")
async def ask_question(session_id: str = Depends(get_session)):
    try:
        # Get the current question index for this session
        question_index = cache[session_id]['question_index']

        # If all questions have been asked, return a specific message
        if question_index >= len(preset_questions):
            return {"message": "All questions asked"}

        # Get the question to ask
        question_to_ask = preset_questions[question_index]

        # Update the question index for the next call
        cache[session_id]['question_index'] += 1

        return {"question": question_to_ask}

    except Exception as e:
        logging.exception("Unexpected error occurred.")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/answer")
async def post_answer(answer: str, session_id: str = Depends(get_session)):
    try:
        if not answer:
            logging.error("Answer cannot be empty.")
            raise HTTPException(status_code=400, detail="Answer cannot be empty.")

        # Get the current situation and history from the cache
        situation = cache[session_id]['situation']
        history = cache[session_id]['history']

        # Process the answer and update the situation and history
        new_situation, new_history = await dai.process_question_answer(preset_questions[cache[session_id]['question_index'] - 1], answer)

        # Update the situation and history in the cache
        cache[session_id]['situation'] = new_situation
        cache[session_id]['history'] = new_history

        return {"message": "Answer received and processed"}

    except Exception as e:
        logging.exception("Unexpected error occurred.")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})

@app.post("/generate")
async def generate_statements(session_id: str = Depends(get_session)):
    try:
        # Get the situation and history from the cache
        situation = cache[session_id]['situation']
        history = cache[session_id]['history']

        if not situation or not history:
            return JSONResponse(status_code=400, content={"message": "No situation or history found. Please go through the questions first."})

        # Generate the pickup lines
        pickup_lines = await dai.generate_pickup_lines(situation, history)

        # Clear the situation and history from the cache for the next use
        cache[session_id]['situation'] = None
        cache[session_id]['history'] = None

        return {"pickup_lines": pickup_lines}

    except Exception as e:
        logging.exception("Unexpected error occurred.")
        return JSONResponse(status_code=500, content={"message": f"Unexpected error occurred: {str(e)}"})
