import os
from google.cloud import storage

def list_blobs():
    bucket_name = "vertexai-l300-capstone-dev-handoff"
    print(f"Listing blobs in bucket: {bucket_name}")
    client = storage.Client()
    bucket = client.bucket(bucket_name)
    blobs = list(bucket.list_blobs())
    # Sort by time created (newest first)
    blobs.sort(key=lambda b: b.time_created, reverse=True)
    for blob in blobs[:15]:
        print(f"- {blob.name} (Created: {blob.time_created})")

if __name__ == "__main__":
    list_blobs()
