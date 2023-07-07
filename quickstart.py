import os
import re
import dai
import base64
import uuid
import uvicorn
import asyncio
import logging
import aiohttp
import aiofiles
from typing import List
from io import BytesIO
from fastapi import UploadFile
from PIL import Image, UnidentifiedImageError
from fastapi import FastAPI, UploadFile, File, HTTPException, BackgroundTasks, Depends
from azure.storage.blob import BlobServiceClient, BlobBlock
from azure.core.exceptions import ResourceNotFoundError
from azure.storage.blob import BlobServiceClient
from slowapi import Limiter
from datetime import datetime
from azure.cognitiveservices.vision.computervision import ComputerVisionClient
from msrest.authentication import CognitiveServicesCredentials
from slowapi.util import get_remote_address
from slowapi.errors import RateLimitExceeded
from fastapi.responses import PlainTextResponse
from pillow_heif import register_heif_opener
from starlette.datastructures import UploadFile as StarletteUploadFile

logging.basicConfig(level=logging.DEBUG)  # Change logging level to DEBUG
logger = logging.getLogger(__name__)

register_heif_opener()

COMPUTER_VISION_KEY = os.getenv("COMPUTER_VISION_KEY")
COMPUTER_VISION_ENDPOINT = os.getenv("COMPUTER_VISION_ENDPOINT")
# Correctly using os.getenv to get the environment variable
BLOB_CONNECTION_STRING = os.getenv("BLOB_CONNECTION_STRING")

credentials = CognitiveServicesCredentials(COMPUTER_VISION_KEY)
client = ComputerVisionClient(
    endpoint=COMPUTER_VISION_ENDPOINT,
    credentials=credentials
)

limiter = Limiter(key_func=get_remote_address)

app = FastAPI()

app.state.limiter = limiter

@app.exception_handler(RateLimitExceeded)
async def custom_rate_limit_handler(request, exc):
    return PlainTextResponse("Too many requests", status_code=429)

@app.on_event("startup")
async def startup_event():
    logger.info("Application startup")
    app.state.session = aiohttp.ClientSession()

@app.on_event("shutdown")
async def shutdown_event():
    logger.info("Application shutdown")
    await app.state.session.close()

async def convert_image(file: UploadFile):
    logger.info(f"Converting image: {file.filename}")
    try:
        image = Image.open(file.file)
        if file.content_type == "image/heif":
            file_like_object = BytesIO()
            image.save(file_like_object, format="JPEG")
            file_like_object.seek(0)
            file.file = file_like_object
            file.filename = file.filename.rsplit(".", 1)[0] + ".jpeg"
    except UnidentifiedImageError:
        logger.error(f"Failed to open the image file: {file.filename}")
        raise HTTPException(status_code=415, detail="Invalid image file")
    except Exception as e:
        logger.error(f"Failed to convert image: {str(e)}")
        raise HTTPException(status_code=415, detail=f"Could not process the file: {str(e)}")
    
    logger.info(f"Image conversion completed: {file.filename}")
    return UploadFile(
        filename=file.filename,
        file=file.file
    )

async def upload_image_to_blob(file: UploadFile):
    container_name = "images"
    unique_id = uuid.uuid4()
    timestamp = datetime.now().strftime("%Y%m%d%H%M%S")

    # Sanitize filename
    safe_filename = re.sub('[^a-zA-Z0-9\.\-_]', '_', file.filename)
    blob_name = f"{timestamp}_{unique_id}_{safe_filename}"

    blob_service_client = BlobServiceClient.from_connection_string(BLOB_CONNECTION_STRING)
    container_client = blob_service_client.get_container_client(container_name)  # Get container client
    blob_client = container_client.get_blob_client(blob_name)

    logger.info(f"Checking if the blob: {blob_name} already exists")
    try:
        blob_client.get_blob_properties()
        logger.warning(f"File: {file.filename} already exists in blob storage.")
        raise HTTPException(status_code=409, detail="File already exists in the blob storage.")
    except ResourceNotFoundError:
        pass

    logger.info(f"Starting to upload file: {file.filename} to blob: {blob_name}")
    block_list = []
    try:
        chunk_size = 1024 * 1024 * 4  # 4MB
        block_id = 0
        file.file.seek(0)

        while True:
            data = await file.read(chunk_size)
            if not data:
                break

            block_id_str = base64.b64encode(uuid.uuid4().bytes).decode()
            blob_client.stage_block(block_id_str, data)
            block_list.append(BlobBlock(block_id=block_id_str))  # Make sure BlobBlock objects are added

            block_id += 1

        blob_client.commit_block_list(block_list)
        logger.info(f"Successfully uploaded file: {file.filename} to blob: {blob_name}")

    except Exception as e:
        logger.error(f"Failed to upload image to blob: {str(e)}")
        raise HTTPException(status_code=500, detail=f"Could not upload file to blob: {str(e)}")

    return blob_client.url

@app.post("/uploadfiles/", dependencies=[Depends(limiter.limit("5/minute"))])
async def create_upload_file(file: UploadFile = File(...)):
    ALLOWED_TYPES: List[str] = ["image/jpeg", "image/png", "image/heif"]
    if file.content_type not in ALLOWED_TYPES:
        logger.error(f"Invalid file type for file: {file.filename}")
        raise HTTPException(status_code=400, detail="Invalid file type. Only JPEG, PNG, and HEIF types are allowed.")
    
    logger.info(f"Received file: {file.filename} for upload and conversion")
    try:
        file = await convert_image(file)
        image_url = await upload_image_to_blob(file)
    except Exception as e:
        logger.error(f"Failed to upload and convert image: {str(e)}")
        generic_error_message = "An error occurred. Please try again later."
        raise HTTPException(status_code=500, detail=generic_error_message)

    image_dict = {"image_url": image_url, "file_name": file.filename}

    logger.info(f"Sending processed image data to DAI service")
    try:
        response = await dai.receive_uploaded_image(image_dict)
        return response
    except Exception as e:
        logger.error(f"Failed to send image data to DAI service: {str(e)}")
        raise HTTPException(
            status_code=500,
            detail=generic_error_message
        )

logger.info("Service is running...")