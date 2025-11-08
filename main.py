from fastapi import FastAPI, File, UploadFile, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from google import genai
from pydantic import BaseModel
import psycopg2
import json
from datetime import datetime
import os
from dotenv import load_dotenv
import tempfile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
import time
import gspread
from oauth2client.service_account import ServiceAccountCredentials
from gspread_formatting import *


load_dotenv()  # load environment variables if present

# --- Config ---
DB_HOST = os.getenv("DB_HOST")
DB_NAME = os.getenv("DB_NAME")
DB_USER = os.getenv("DB_USER")
DB_PASS = os.getenv("DB_PASS")

SHEET_NAME = "Family_Library_Books"

scope = [
    "https://spreadsheets.google.com/feeds",
    "https://www.googleapis.com/auth/drive",
    "https://www.googleapis.com/auth/spreadsheets"
]
creds = ServiceAccountCredentials.from_json_keyfile_name("credentials.json", scope)
client = gspread.authorize(creds)
sheet = client.open(SHEET_NAME)

# Check for missing environment variables
if not all([DB_HOST, DB_NAME, DB_USER, DB_PASS]):
    raise Exception("Missing one or more required database environment variables")

app = FastAPI(title="Home Library Backend")

# --- CORS Middleware ---
# Allow all origins for local development.
# For production, you should restrict this to your frontend's domain.
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allows all origins
    allow_credentials=True,
    allow_methods=["*"],  # Allows all methods
    allow_headers=["*"],  # Allows all headers
)
app.mount("/static", StaticFiles(directory="static"), name="static")
# --- Pydantic model for Gemini schema ---
class Books(BaseModel):
    title: str
    genre: str

# --- API Route ---
@app.post("/upload-book/")
async def upload_book(file: UploadFile = File(...)):
    try:
        # Use a temporary file for the upload
        with tempfile.NamedTemporaryFile(delete=False, suffix=os.path.splitext(file.filename)[1]) as tmp:
            tmp.write(await file.read())
            temp_path = tmp.name

        # Upload to Gemini
        client = genai.Client(api_key=os.getenv("GEMINI_API_KEY"))
        my_file = client.files.upload(file=temp_path)

        prompt = (
            "Based on the given image identify the details of the book "
            "and provide the response in JSON format with fields title and genre as given in the output schema."
        )

        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=[my_file, prompt],
            config={
                "response_mime_type": "application/json",
                "response_json_schema": Books.model_json_schema(),
            },
        )

        print("Gemini raw output:", response.text)
        book_data = json.loads(response.text)

        # --- Insert into PostgreSQL ---
        conn = psycopg2.connect(
            host=DB_HOST, database=DB_NAME, user=DB_USER, password=DB_PASS
        )
        cur = conn.cursor()
        
        # Use parameterized query to prevent SQL injection
        query = "INSERT INTO books (title, genre) VALUES (%s, %s) RETURNING id;"
        cur.execute(query, (book_data['title'], book_data['genre']))
        
        #get the id of the inserted row
        new_id = cur.fetchone()[0]

        conn.commit()
        cur.close()
        conn.close()

        # --- Add to Google Sheet ---
        worksheet = sheet.worksheet("Sheet1")
        added_at = datetime.now().strftime("%m/%d/%Y %H:%M:%S")
        row = [new_id, book_data['title'], book_data['genre'], added_at]
        worksheet.append_row(row)
        
        return {
            "status": "success",
            "book": book_data,
            "message": "Book inserted successfully!"
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        # Clean up the temporary file
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)


# --- Health Check Route ---
@app.get("/")
def serve_index():
    return FileResponse("static/index.html")