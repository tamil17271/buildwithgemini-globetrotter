# ruff: noqa
# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     https://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

import datetime
import json
import os
from pathlib import Path
from zoneinfo import ZoneInfo
from google.cloud import firestore

from a2ui.basic_catalog.provider import BasicCatalog
from a2ui.schema.manager import A2uiSchemaManager
from google.adk.agents import Agent
from google.adk.agents.callback_context import CallbackContext
from google.adk.apps import App
from google.adk.code_executors import AgentEngineSandboxCodeExecutor
from google.adk.memory import VertexAiMemoryBankService
from google.adk.models import Gemini
from google.adk.tools import ToolContext
from google.adk.tools.preload_memory_tool import PreloadMemoryTool
from google.genai import types

from .a2ui_utils import a2ui_callback


MODEL = "gemini-3.8-flash"

# CRITICAL: Hardcoded GCP project ID and GCS bucket name strings for Agent Platform compatibility
PROJECT_ID = "qwiklabs-gcp-03-380895049416"
COLLECTION_NAME = "destinations"
BUCKET_NAME = "globetrotter-assets-380895049416"

# Load Agent Engine deployment metadata for sandbox code execution
DEPLOYMENT_METADATA_PATH = Path(__file__).resolve().parent.parent / "deployment_metadata.json"
remote_agent_runtime_id = None
sandbox_resource_name = None

if DEPLOYMENT_METADATA_PATH.exists():
    try:
        with open(DEPLOYMENT_METADATA_PATH, "r") as f:
            metadata = json.load(f)
            remote_agent_runtime_id = metadata.get("remote_agent_runtime_id")
            sandbox_resource_name = metadata.get("sandbox_resource_name")
    except Exception:
        pass

code_executor = AgentEngineSandboxCodeExecutor(
    sandbox_resource_name=sandbox_resource_name,
    agent_engine_resource_name=remote_agent_runtime_id,
)


def _get_firestore_client() -> firestore.Client:
    return firestore.Client(project=PROJECT_ID)


def search_destinations(query: str = "", category: str = "") -> list[dict]:
    """Search travel destinations and accommodations in the Firestore catalog.

    Args:
        query: Optional search string matching destination name or location (e.g. 'Kyoto', 'Italy').
        category: Optional category filter (e.g. 'Accommodation', 'Cultural Spot', 'Activity').

    Returns:
        A list of matching destination dictionaries.
    """
    db = _get_firestore_client()
    docs = db.collection(COLLECTION_NAME).stream()
    results = []
    q_lower = query.lower() if query else ""
    c_lower = category.lower() if category else ""

    for doc in docs:
        data = doc.to_dict()
        name = str(data.get("name", "")).lower()
        location = str(data.get("location", "")).lower()
        cat = str(data.get("category", "")).lower()

        matches_query = not q_lower or (q_lower in name or q_lower in location)
        matches_cat = not c_lower or (c_lower in cat)

        if matches_query and matches_cat:
            results.append(data)

    return results


def add_destination(
    destination_id: str,
    name: str,
    location: str,
    category: str,
    description: str,
    price_per_night: float,
    tags: list[str],
) -> str:
    """Add a new destination or accommodation item to the Firestore catalog.

    Args:
        destination_id: Unique string identifier for the destination (e.g. 'dest-006').
        name: Name of the destination or hotel.
        location: City and country (e.g. 'Paris, France').
        category: Category type (e.g. 'Accommodation', 'Cultural Spot', 'Activity').
        description: Detailed description of the venue or experience.
        price_per_night: Price per night or entry in USD.
        tags: List of keyword tags (e.g. ['romantic', 'museum']).

    Returns:
        Confirmation message with destination ID.
    """
    db = _get_firestore_client()
    doc_data = {
        "id": destination_id,
        "name": name,
        "location": location,
        "category": category,
        "description": description,
        "price_per_night": float(price_per_night),
        "rating": 5.0,
        "tags": tags if isinstance(tags, list) else [tags],
    }
    db.collection(COLLECTION_NAME).document(destination_id).set(doc_data)
    return f"Successfully added destination '{name}' (ID: '{destination_id}') to Firestore."


def calculate_trip_budget(
    destination_id: str,
    duration_days: int = 3,
    travelers_count: int = 1,
) -> dict:
    """Calculate the estimated trip budget for a destination stored in Firestore.

    Args:
        destination_id: Unique string identifier for the destination (e.g. 'dest-001').
        duration_days: Number of days/nights for the trip.
        travelers_count: Number of travelers in the group.

    Returns:
        A dictionary containing the itemized budget breakdown and total estimated cost.
    """
    db = _get_firestore_client()
    doc = db.collection(COLLECTION_NAME).document(destination_id).get()

    if not doc.exists:
        return {"error": f"Destination with ID '{destination_id}' not found in Firestore."}

    data = doc.to_dict()
    price_per_night = float(data.get("price_per_night", 0.0))
    lodging_total = price_per_night * duration_days
    estimated_meals_per_day = 50.0 * travelers_count
    total_meals = estimated_meals_per_day * duration_days
    total_cost = lodging_total + total_meals

    return {
        "destination_name": data.get("name"),
        "location": data.get("location"),
        "duration_days": duration_days,
        "travelers_count": travelers_count,
        "lodging_total_usd": lodging_total,
        "estimated_meals_usd": total_meals,
        "total_estimated_cost_usd": total_cost,
        "cost_per_person_usd": round(total_cost / max(1, travelers_count), 2),
    }


def get_currency_exchange_rates(
    base_currency: str = "USD",
    target_currency: str = "",
) -> dict:
    """Fetch live real-time currency exchange rates from a public exchange rate API.

    Args:
        base_currency: The 3-letter base currency code (e.g. 'USD', 'EUR', 'GBP').
        target_currency: Optional specific 3-letter currency code to check (e.g. 'JPY', 'EUR').

    Returns:
        A dictionary containing exchange rate information or error details.
    """
    import json
    import os
    import urllib.request

    api_key = os.environ.get("EXCHANGE_RATE_API_KEY")
    base = base_currency.upper().strip()

    if api_key:
        url = f"https://v6.exchangerate-api.com/v6/{api_key}/latest/{base}"
    else:
        url = f"https://open.er-api.com/v6/latest/{base}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GlobeTrotter/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("result") != "success":
            return {"error": f"Failed to retrieve rates for currency '{base}'."}

        rates = data.get("rates", {})
        if target_currency:
            target = target_currency.upper().strip()
            rate = rates.get(target)
            if rate is not None:
                return {
                    "base_currency": base,
                    "target_currency": target,
                    "rate": rate,
                    "last_update": data.get("time_last_update_utc"),
                }
            return {"error": f"Target currency '{target}' not found."}

        popular_currencies = ["EUR", "JPY", "GBP", "CAD", "AUD", "CHF", "CNY", "BRL"]
        filtered_rates = {
            code: rates[code] for code in popular_currencies if code in rates
        }

        return {
            "base_currency": base,
            "rates": filtered_rates,
            "last_update": data.get("time_last_update_utc"),
        }
    except Exception as e:
        return {"error": f"Currency API request failed: {str(e)}"}


def geocode_address(address: str) -> dict:
    """Convert an address string or landmark name into geographic coordinates (latitude & longitude) using Google Maps Geocoding API.

    Args:
        address: Street address, landmark, or city (e.g. 'Kyoto Station', 'Eiffel Tower, Paris').

    Returns:
        A dictionary containing formatted address, latitude, longitude, and place ID.
    """
    import json
    import os
    import urllib.parse
    import urllib.request
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key == "PASTE_KEY_HERE":
        return {"error": "GOOGLE_MAPS_API_KEY is not configured in .env. Please set a valid Google Maps API key."}

    encoded_address = urllib.parse.quote(address)
    url = f"https://maps.googleapis.com/maps/api/geocode/json?address={encoded_address}&key={api_key}"

    try:
        req = urllib.request.Request(url, headers={"User-Agent": "GlobeTrotter-Agent/1.0"})
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        if data.get("status") != "OK" or not data.get("results"):
            return {"error": f"Geocoding failed for '{address}'. Status: {data.get('status')}"}

        first = data["results"][0]
        loc = first["geometry"]["location"]
        return {
            "formatted_address": first.get("formatted_address"),
            "latitude": loc.get("lat"),
            "longitude": loc.get("lng"),
            "place_id": first.get("place_id"),
        }
    except Exception as e:
        return {"error": f"Geocoding API request error: {str(e)}"}


def find_nearby_places(
    latitude: float,
    longitude: float,
    place_type: str = "tourist_attraction",
    max_results: int = 5,
) -> dict:
    """Find nearby places or attractions of a given type around coordinates using Places API (New).

    Args:
        latitude: Geographic latitude center point (e.g. 35.02107).
        longitude: Geographic longitude center point (e.g. 135.75385).
        place_type: Category of place to search for (e.g. 'tourist_attraction', 'restaurant', 'hotel', 'museum').
        max_results: Maximum number of places to return (default 5, max 20).

    Returns:
        A dictionary containing matching places with name, address, and location coordinates.
    """
    import json
    import os
    import urllib.request
    from dotenv import load_dotenv

    load_dotenv()
    api_key = os.environ.get("GOOGLE_MAPS_API_KEY")
    if not api_key or api_key == "PASTE_KEY_HERE":
        return {"error": "GOOGLE_MAPS_API_KEY is not configured in .env. Please set a valid Google Maps API key."}

    url = "https://places.googleapis.com/v1/places:searchNearby"
    headers = {
        "Content-Type": "application/json",
        "X-Goog-Api-Key": api_key,
        "X-Goog-FieldMask": "places.displayName,places.formattedAddress,places.location,places.types,places.rating",
    }

    payload = {
        "includedTypes": [place_type],
        "maxResultCount": min(max_results, 20),
        "locationRestriction": {
            "circle": {
                "center": {"latitude": latitude, "longitude": longitude},
                "radius": 5000.0,
            }
        },
    }

    try:
        body = json.dumps(payload).encode("utf-8")
        req = urllib.request.Request(url, data=body, headers=headers, method="POST")
        with urllib.request.urlopen(req, timeout=5) as resp:
            data = json.loads(resp.read().decode("utf-8"))

        raw_places = data.get("places", [])
        results = []
        for p in raw_places:
            disp = p.get("displayName", {})
            results.append({
                "name": disp.get("text") if isinstance(disp, dict) else str(disp),
                "address": p.get("formattedAddress"),
                "location": p.get("location"),
                "rating": p.get("rating"),
                "types": p.get("types", []),
            })

        return {
            "search_center": {"latitude": latitude, "longitude": longitude},
            "place_type": place_type,
            "places_found": len(results),
            "places": results,
        }
    except Exception as e:
        return {"error": f"Places API (New) request error: {str(e)}"}


def generate_destination_image(
    prompt: str,
    tool_context: ToolContext,
) -> dict:
    """Generate an image for a travel destination or landmark using gemini-3.1-flash-lite-image model in global region.

    Saves the generated image as an ADK artifact (Playground panel) and uploads it directly to public Cloud Storage.

    Args:
        prompt: Detailed description of the destination or landscape to render (e.g. 'Kyoto Serenity Ryokan during spring cherry blossom season').
        tool_context: ADK ToolContext provided automatically by the framework.

    Returns:
        A dictionary containing the image filename, public Cloud Storage HTTPS URL, and status.
    """
    import uuid
    from google.cloud import storage
    import google.genai
    from google.genai import types

    genai_client = google.genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location="global",
    )

    try:
        response = genai_client.models.generate_content(
            model="gemini-3.1-flash-lite-image",
            contents=f"Generate a travel photo of: {prompt}",
        )

        image_bytes = None
        mime_type = "image/jpeg"

        if response.candidates and response.candidates[0].content.parts:
            for part in response.candidates[0].content.parts:
                if hasattr(part, "inline_data") and part.inline_data:
                    image_bytes = part.inline_data.data
                    if part.inline_data.mime_type:
                        mime_type = part.inline_data.mime_type
                    break

        if not image_bytes:
            return {"error": "Failed to generate image bytes from gemini-3.1-flash-lite-image model response."}

        filename = f"destination_{uuid.uuid4().hex[:8]}.jpg"

        # 1. Save with tool_context.save_artifact for Playground Artifacts panel
        artifact_part = types.Part.from_bytes(data=image_bytes, mime_type=mime_type)
        tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload image bytes directly to public Cloud Storage bucket (no local file writing)
        storage_client = storage.Client(project=PROJECT_ID)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(image_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"

        return {
            "filename": filename,
            "public_url": public_url,
            "prompt": prompt,
            "status": "Image generated successfully, saved to Playground artifacts, and published to Cloud Storage.",
        }

    except Exception as e:
        return {"error": f"Image generation error: {str(e)}"}


def generate_destination_video(
    prompt: str,
    tool_context: ToolContext,
) -> dict:
    """Generate a short video for a travel destination or location highlight using Google's Omni model (gemini-omni-flash-preview) in global region.

    Saves the generated video bytes as an ADK artifact (Playground panel) and uploads it directly to public Cloud Storage.

    Args:
        prompt: Description of the destination scene or travel highlight to render (e.g. 'Scenic sunset over Mount Fuji with cherry blossoms blowing in the wind').
        tool_context: ADK ToolContext provided automatically by the framework.

    Returns:
        A dictionary containing the video filename, public Cloud Storage HTTPS URL, and status.
    """
    import base64
    import uuid
    from google.cloud import storage
    import google.genai
    from google.genai import types

    genai_client = google.genai.Client(
        vertexai=True,
        project=PROJECT_ID,
        location="global",
    )

    try:
        interaction = genai_client.interactions.create(
            model="gemini-omni-flash-preview",
            input=f"Generate a short video clip of travel destination highlight: {prompt}",
        )

        video_obj = getattr(interaction, "output_video", None)
        if not video_obj or not getattr(video_obj, "data", None):
            return {"error": "Failed to generate video bytes from gemini-omni-flash-preview interaction response."}

        data = video_obj.data
        if isinstance(data, str):
            video_bytes = base64.b64decode(data)
        else:
            video_bytes = data

        mime_type = getattr(video_obj, "mime_type", None) or "video/mp4"
        filename = f"destination_video_{uuid.uuid4().hex[:8]}.mp4"

        # 1. Save with tool_context.save_artifact for Playground Artifacts panel
        artifact_part = types.Part.from_bytes(data=video_bytes, mime_type=mime_type)
        tool_context.save_artifact(filename=filename, artifact=artifact_part)

        # 2. Upload video bytes directly to public Cloud Storage bucket (no local file writing)
        storage_client = storage.Client(project=PROJECT_ID)
        bucket = storage_client.bucket(BUCKET_NAME)
        blob = bucket.blob(filename)
        blob.upload_from_string(video_bytes, content_type=mime_type)

        public_url = f"https://storage.googleapis.com/{BUCKET_NAME}/{filename}"

        return {
            "filename": filename,
            "public_url": public_url,
            "prompt": prompt,
            "status": "Video generated successfully, saved to Playground artifacts, and published to Cloud Storage.",
        }

    except Exception as e:
        return {"error": f"Video generation error: {str(e)}"}


def get_weather(query: str) -> str:
    """Simulates a web search for weather information.

    Args:
        query: Location string to check weather for.

    Returns:
        Simulated weather information.
    """
    if "sf" in query.lower() or "san francisco" in query.lower():
        return "It's 60 degrees and foggy."
    return "It's 90 degrees and sunny."


MEMORY_BANK_ID = "1596521120100515840"


def memory_bank_service_builder():
    return VertexAiMemoryBankService(
        project=PROJECT_ID,
        location="us-central1",
        agent_engine_id=MEMORY_BANK_ID,
    )


async def generate_memories_callback(callback_context: CallbackContext):
    try:
        await callback_context.add_session_to_memory()
    except Exception as e:
        print(f"Memory callback warning: {e}")
    return None


schema_manager = A2uiSchemaManager(
    version="0.8",
    catalogs=[BasicCatalog.get_config("0.8")],
)

a2ui_instruction = schema_manager.generate_system_prompt(
    role_description=(
        "You are GlobeTrotter, an AI Travel & Itinerary Concierge. Help users discover travel destinations, "
        "generate travel images and videos, geocode locations, search nearby attractions using Google Maps, calculate trip budgets, "
        "check live exchange rates, execute sandboxed code calculations, and manage destination catalog items stored in Firestore. "
        "Always recall, track, and remember all stated user allergies (food, environmental, or medical) and dietary preferences "
        "across conversations, ensuring every travel, restaurant, and accommodation recommendation strictly respects these safety requirements."
    ),
    workflow_description="Analyze the travel request, invoke tools when needed, and return structured UI when appropriate.",
    ui_description=(
        "Keep every surface tiny and flat: ONE Card > ONE Column > a few Text rows. "
        "Never nest a Card inside a Card. "
        "Use ONLY these components: Card, Column, Row, Text, and Image. Do not use "
        "Table or Heading (unsupported), or Buttons, actions, or forms (they do "
        "nothing in adk web). "
        "You may include one Image component, but only when you have a public https "
        "URL for the image (for example the URL an image tool returns after uploading "
        "to a public bucket). Set the Image url to that exact https link, for example "
        '{"Image": {"url": {"literalString": "https://..."}}}. Never point an '
        "Image at a bare filename, an artifact name, or a non-http(s) path. If you do "
        "not have a public URL, add a short Text line noting the image instead. "
        "No markdown in text; use the usageHint property ('h1', 'h2', 'body') for "
        "headings and emphasis. "
        "Output ONLY the raw A2UI JSON array — no prose, and never wrap it in "
        "<a2a_datapart_json> tags or 'kind'/'data'/'metadata' objects."
    ),
    include_schema=True,
    include_examples=True,
)


root_agent = Agent(
    # Keep in sync with agents-cli-manifest.yaml
    name="globetrotter",
    model=Gemini(
        model=MODEL,
        retry_options=types.HttpRetryOptions(attempts=3),
    ),
    instruction=a2ui_instruction,
    code_executor=code_executor,
    after_agent_callback=generate_memories_callback,
    after_model_callback=a2ui_callback,
    tools=[
        PreloadMemoryTool(),
        search_destinations,
        add_destination,
        calculate_trip_budget,
        get_currency_exchange_rates,
        geocode_address,
        find_nearby_places,
        generate_destination_image,
        generate_destination_video,
        get_weather,
    ],
)

app = App(
    root_agent=root_agent,
    name="app",
)

