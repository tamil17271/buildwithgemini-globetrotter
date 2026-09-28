import sys
from google.cloud import firestore

# CRITICAL: Hardcoded GCP project ID string to ensure compatibility with Agent Platform
PROJECT_ID = "qwiklabs-gcp-03-380895049416"
COLLECTION_NAME = "destinations"

SEED_DESTINATIONS = [
    {
        "id": "dest-001",
        "name": "Kyoto Serenity Ryokan",
        "location": "Kyoto, Japan",
        "category": "Accommodation",
        "price_per_night": 280,
        "rating": 4.9,
        "tags": ["onsen", "culture", "luxury", "ryokan"],
        "description": "Tranquil ryokan featuring private open-air onsen baths and multi-course traditional kaiseki dining.",
    },
    {
        "id": "dest-002",
        "name": "Fushimi Inari Taisha Shrine",
        "location": "Kyoto, Japan",
        "category": "Cultural Spot",
        "price_per_night": 0,
        "rating": 4.8,
        "tags": ["shrine", "hiking", "culture", "free"],
        "description": "Iconic Shinto shrine famous for thousands of vermilion torii gates winding up Mount Inari.",
    },
    {
        "id": "dest-003",
        "name": "Amalfi Coast Cliffside Villa",
        "location": "Amalfi, Italy",
        "category": "Accommodation",
        "price_per_night": 450,
        "rating": 4.9,
        "tags": ["sea-view", "luxury", "coastal", "villa"],
        "description": "Luxury cliffside suite offering panoramic Tyrrhenian Sea views and private lemon grove garden.",
    },
    {
        "id": "dest-004",
        "name": "Santorini Sunset Sailing Excursion",
        "location": "Santorini, Greece",
        "category": "Activity",
        "price_per_night": 160,
        "rating": 4.7,
        "tags": ["sailing", "sunset", "catamaran", "tour"],
        "description": "Catamaran cruise around Oia caldera with snorkeling at volcanic hot springs and gourmet dinner.",
    },
    {
        "id": "dest-005",
        "name": "Machu Picchu Citadel Guided Tour",
        "location": "Cusco, Peru",
        "category": "Activity",
        "price_per_night": 220,
        "rating": 4.95,
        "tags": ["historical", "hiking", "unesco", "wonder"],
        "description": "Full-day guided exploration of the ancient Incan citadel with panoramic Andean mountain views.",
    },
]


def seed_firestore():
    print(f"Connecting to Firestore for project: {PROJECT_ID}...")
    db = firestore.Client(project=PROJECT_ID)
    collection_ref = db.collection(COLLECTION_NAME)

    for item in SEED_DESTINATIONS:
        doc_ref = collection_ref.document(item["id"])
        doc_ref.set(item)
        print(f"Seeded destination: {item['name']} (ID: {item['id']})")

    print(f"Successfully seeded {len(SEED_DESTINATIONS)} items into collection '{COLLECTION_NAME}'!")


if __name__ == "__main__":
    seed_firestore()
