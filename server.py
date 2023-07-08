import logging
import puremagic

from fastapi import FastAPI, HTTPException, UploadFile, File
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
import dai
import quickstart

app = FastAPI()
app = FastAPI(debug=True)

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

# Initialize global variables for situation and history
situation_global = None
history_global = None

@app.get("/")
async def root():
    return {"message": "Welcome to my API!"}

@app.post("/upload")
async def image_upload(image: UploadFile = File(...)):
    logging.info(f"Image details: Filename - {image.filename}, Content-Type - {image.content_type}")

    try:
        # File type validation
        file_content = await image.read()  # python-magic call
        await image.seek(0)  # Reset file pointer to start
        file_type = puremagic.magic_buffer(file_content)
        
        if not any(mime.mime.startswith('image/') for mime in file_type):
            logging.error("Invalid file type.")
            raise HTTPException(
                status_code=400, 
                detail="Invalid file type. Please upload an image file."
            )

        # Image size validation
        content = await image.read()
        if len(content) > 1e6:  # Larger than 1 MB
            logging.error("Image file size is too large.")
            raise HTTPException(
                status_code=400,
                detail="Image file size is too large. Please upload a smaller image."
            )
        await image.seek(0)  # Reset file pointer to start

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
