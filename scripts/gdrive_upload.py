"""Upload a single file into a named child folder on Google Drive via OAuth.

Invoked from GitHub Actions steps so CI backups avoid the Service Account
storage quota. The child folder is resolved by name under the shared parent;
if it doesn't exist yet, it is created once, then reused on subsequent runs.

Upload strategy: two-step (create metadata + parent, then PATCH content).
The Drive multipart endpoint expects `multipart/related`, not the
`multipart/form-data` that `requests.post(..., files=...)` produces, so the
metadata part silently gets dropped and files end up orphaned in the OAuth
user's root My Drive. Splitting the request into a JSON create + a media
PATCH sidesteps that entirely and also surfaces any permission problem on
whichever half actually fails.

Required env vars:
  CLIENT_ID, CLIENT_SECRET, REFRESH_TOKEN  OAuth app credentials.
  FOLDER_ID     Drive ID of the shared parent folder.
  CHILD_FOLDER  Name of the child folder (resolved or created under FOLDER_ID).
  FILE_PATH     Absolute path of the local file to upload.
  FILE_NAME     Name to give the uploaded file on Drive.
"""
import json
import os
import sys

import requests

TOKEN_URL = "https://oauth2.googleapis.com/token"
DRIVE_FILES_URL = "https://www.googleapis.com/drive/v3/files"
DRIVE_MEDIA_URL = "https://www.googleapis.com/upload/drive/v3/files/{file_id}?uploadType=media"
FOLDER_MIME = "application/vnd.google-apps.folder"


def get_access_token() -> str:
    res = requests.post(TOKEN_URL, data={
        "client_id": os.environ["CLIENT_ID"],
        "client_secret": os.environ["CLIENT_SECRET"],
        "refresh_token": os.environ["REFRESH_TOKEN"],
        "grant_type": "refresh_token",
    })
    token = res.json().get("access_token")
    if not token:
        print("Failed to get access token:", res.text)
        sys.exit(1)
    return token


def resolve_or_create_child(headers: dict, parent_id: str, child_name: str) -> str:
    # Escape any apostrophes in the folder name so the Drive query parser
    # doesn't truncate it mid-literal.
    escaped_name = child_name.replace("'", r"\'")
    query = (
        f"name = '{escaped_name}' and "
        f"mimeType = '{FOLDER_MIME}' and "
        f"'{parent_id}' in parents and trashed = false"
    )
    # supportsAllDrives / includeItemsFromAllDrives cover the case where the
    # shared parent's ownership differs from the OAuth user's — without them
    # the folder the script created on a previous run can fail to show up in
    # the search, so every run creates a duplicate.
    search = requests.get(
        DRIVE_FILES_URL,
        headers=headers,
        params={
            "q": query,
            "fields": "files(id, name, parents)",
            "spaces": "drive",
            "supportsAllDrives": "true",
            "includeItemsFromAllDrives": "true",
        },
    )
    if search.status_code >= 300:
        print(f"Folder search failed ({search.status_code}):", search.text)
        sys.exit(1)
    matches = search.json().get("files", [])
    print(f"Search for {child_name!r} in parent {parent_id}: {len(matches)} match(es)")
    if matches:
        child_id = matches[0]["id"]
        print(f"Using existing child folder {child_name}: {child_id}")
        return child_id

    create = requests.post(
        DRIVE_FILES_URL,
        headers={**headers, "Content-Type": "application/json"},
        params={"supportsAllDrives": "true"},
        data=json.dumps({
            "name": child_name,
            "mimeType": FOLDER_MIME,
            "parents": [parent_id],
        }),
    )
    if create.status_code >= 300:
        print(f"Failed to create child folder ({create.status_code}):", create.text)
        sys.exit(1)
    child_id = create.json().get("id")
    if not child_id:
        print("Create-folder response missing id:", create.json())
        sys.exit(1)
    print(f"Created child folder {child_name}: {child_id}")
    return child_id


def upload_into_folder(headers: dict, folder_id: str, file_name: str, file_path: str) -> None:
    create = requests.post(
        DRIVE_FILES_URL,
        headers={**headers, "Content-Type": "application/json"},
        params={"supportsAllDrives": "true"},
        data=json.dumps({"name": file_name, "parents": [folder_id]}),
    )
    if create.status_code >= 300:
        print(f"Failed to create file metadata ({create.status_code}):", create.text)
        sys.exit(1)
    file_id = create.json().get("id")
    if not file_id:
        print("Create-file response missing id:", create.json())
        sys.exit(1)

    with open(file_path, "rb") as fh:
        content = fh.read()
    patch = requests.patch(
        DRIVE_MEDIA_URL.format(file_id=file_id),
        headers={**headers, "Content-Type": "application/octet-stream"},
        params={"supportsAllDrives": "true"},
        data=content,
    )
    if patch.status_code >= 300:
        print(f"Failed to upload content ({patch.status_code}):", patch.text)
        sys.exit(1)
    print(f"Uploaded {file_name} ({len(content)} bytes) as {file_id} into {folder_id}")


def main() -> int:
    headers = {"Authorization": f"Bearer {get_access_token()}"}
    child_id = resolve_or_create_child(
        headers,
        os.environ["FOLDER_ID"],
        os.environ["CHILD_FOLDER"],
    )
    upload_into_folder(
        headers,
        child_id,
        os.environ["FILE_NAME"],
        os.environ["FILE_PATH"],
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
