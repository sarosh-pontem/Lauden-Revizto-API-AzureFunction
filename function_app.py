import azure.functions as func
import logging
import os
import requests
import pandas as pd
from azure.storage.blob import BlobServiceClient
import io

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
CONNECTION_STRING = os.getenv("CONNECTION_STRING")
CONTAINER_NAME = "revizto-api"
BLOB_NAME = "revizto_refresh_token.txt"



@app.route(route="issues")
def issues(req: func.HttpRequest) -> func.HttpResponse:
    try:

        get_issues_for_all_projects()

        return func.HttpResponse("Issues fetched and saved successfully.", status_code=200)
    except Exception as e:
        logging.error(f"Error: {e}")
        return func.HttpResponse(f"An error occurred: {str(e)}", status_code=500)
    

blob_service_client = BlobServiceClient.from_connection_string(CONNECTION_STRING)
blob_client = blob_service_client.get_blob_client(container=CONTAINER_NAME, blob=BLOB_NAME)


REGION = "canada" 
BASE_URL = f"https://api.{REGION}.revizto.com/v5/oauth2"
TOKEN_FILE = "revizto_refresh_token.txt"

API_URL = f"https://api.{REGION}.revizto.com/v5"



## Function to save DataFrame to Azure Blob Storage as CSV
def save_to_blob_storage(df, output_file):
    blob_obj = blob_service_client.get_blob_client(container='revizto-api',blob=output_file)
    csv_buffer = io.StringIO()
    df.to_csv(csv_buffer, index=False)
    csv_buffer.seek(0)
    blob_obj.upload_blob(csv_buffer.getvalue(), overwrite=True)





##Functions to manage refresh token in Azure Blob Storage. This is stored everytime we exchange an Access Code for tokens, or refresh an access token, since the refresh token is updated in both cases. When we need to get a valid access token, we read the current refresh token from blob storage and use it to request a new access token. 
## This way, we can ensure that the function can run independently and always has access to the latest refresh token without relying on local file storage, which is not suitable for Azure Functions.
def save_refresh_token(token: str):

    blob_client.upload_blob(token.strip(), overwrite=True)

##Functions to manage refresh token in Azure Blob Storage. This is stored everytime we exchange an Access Code for tokens, or refresh an access token, since the refresh token is updated in both cases. When we need to get a valid access token, we read the current refresh token from blob storage and use it to request a new access token. 
## This way, we can ensure that the function can run independently and always has access to the latest refresh token without relying on local file storage, which is not suitable for Azure Functions.

def read_refresh_token() -> str:

    try:
        download_stream = blob_client.download_blob()
        return download_stream.readall().decode("utf-8").strip()
    except Exception:
        # Returns empty if the file doesn't exist yet
        return ""


# This function is meant to be run only once, when you first set up the application 
# and have an Access Code from the Revizto web portal. It exchanges the Access Code for an access token and a refresh token,
#  and saves the refresh token to Azure Blob Storage for future use. After running this function once and saving the refresh token,
#  you can rely on the get_valid_access_token() function to handle all future access token requests and refreshes automatically.

# def get_initial_tokens(access_code: str):
    
#     print("Exchanging initial Access Code for tokens...")
#     payload = {
#         "grant_type": "authorization_code",
#         "code": access_code
#     }
#     headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
#     response = requests.post(BASE_URL, data=payload, headers=headers)
    
#     if response.status_code == 200:
#         data = response.json()
#         save_refresh_token(data["refresh_token"])
#         print("Initial token setup successful! Saved refresh token locally.")
#         return data["access_token"]
#     else:
#         raise Exception(f"Failed initial token exchange: {response.text}")


#Reads the current refresh token from Azure Blob Storage and uses it to request a new access token. 
## If the refresh token is missing, it raises an exception prompting the user to run the initial token setup function. 
## If the refresh token has expired or been invalidated, it raises an exception indicating that a new Access Code is needed. Otherwise, it returns the new access token and updates the stored refresh token in Azure Blob Storage.

def get_valid_access_token() -> str:
    
    current_refresh_token = read_refresh_token()
    if not current_refresh_token:
        raise Exception(f"No refresh token found in '{TOKEN_FILE}'. You must run get_initial_tokens() first.")
        
    payload = {
        "grant_type": "refresh_token",
        "refresh_token": current_refresh_token
    }
    headers = {"Content-Type": "application/x-www-form-urlencoded"}
    
    response = requests.post(BASE_URL, data=payload, headers=headers)
    
    if response.status_code == 200:
        data = response.json()

        save_refresh_token(data["refresh_token"])

        logging.info("Access token refreshed successfully.")
        
        return data["access_token"]
    elif "-206" in response.text or response.status_code == 401:
        raise Exception("Your refresh token has expired (older than 1 month) or been invalidated. You need a new Access Code.")
    else:
        raise Exception(f"Failed to refresh token: {response.text}")



def fetch_projects():
    
    project_data = []
    
    ##This is the License UUID for the Revizto account. You can find this in the URL when you log into the web version of Revizto and select a project. It will be in the format of a UUID, and is consistent across all projects under the same account. If you have multiple accounts, each will have its own License UUID.
    LICENSE_UUID = "12da63f7-3e2b-4dae-99bd-26c7cc71cdae"
    
    logging.info("Fetching valid Access Token...")
    access_token = get_valid_access_token()

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    license_projects_url = f"https://api.canada.revizto.com/v5/project/list/{LICENSE_UUID}/paged"
    project_response = requests.get(license_projects_url, headers=headers)

    if project_response.status_code == 200:
        projects = project_response.json()

    
        project_response = projects.get("data", []).get("data", [])
        for project in project_response:


            #check whether the specific project has a tag attached to it
            metaTags = project.get("metaTags", [])

            ##if metaTags is a non-empty list, then has_metaTag will be True, otherwise False
        
            has_metaTag = bool(metaTags)

            project_data.append({
                "project_title": project.get("title"),
                "project_uuid": project.get("uuid"),
                "project_metaTags": project.get("metaTags",[])[0] if has_metaTag else []

            })

    project_df = pd.DataFrame(project_data)

    save_to_blob_storage(project_df, 'revizto_projects.csv')    

    return project_data
                    
def fetch_issues(project_uuid,project_title,access_token):

    issue_list = []

    issue_url = f"https://api.canada.revizto.com/v5/project/{project_uuid}/issue-filter/filter"

    headers = {
        "Accept": "application/json",
        "Authorization": f"Bearer {access_token}",
    }

    issues_response = requests.get(issue_url, headers=headers)

    if issues_response.status_code == 200:
        issues = issues_response.json()
        number_pages = issues.get("data", {}).get("pages", 1)
        
    for page in range(0, number_pages + 1):
        paged_issue_url = f"https://api.canada.revizto.com/v5/project/{project_uuid}/issue-filter/filter?page={page}&sendFullIssueData=True"
        issues_response = requests.get(paged_issue_url, headers=headers)
        if issues_response.status_code == 200:
            issue_per_page = issues_response.json().get("data", {}).get("data", [])

            for issue_item in issue_per_page:
                
                #check whether the specific issue has a sheet attached to it
                sheet_val = issue_item.get("sheet", {}).get("value")
                
                has_sheet = isinstance(sheet_val, dict)
            
                issue_list.append({
                    "project_title":project_title,
                    "uuid": issue_item.get("uuid"),
                    "id": issue_item.get("id"),
                    "author_firstname": issue_item.get("author", {}).get("firstname"),
                    "author_lastname": issue_item.get("author", {}).get("lastname"),
                    "author_email": issue_item.get("author", {}).get("email"),
                    "assignee": issue_item.get("assignee", {}).get("value"),
                    "issue_status": issue_item.get("status", {}).get("value"),
                    "issue_status_timestamp": issue_item.get("status",{}).get("timestamp"),
                    "deadline": issue_item.get("deadline", {}).get("value"),
                    "customTypeName": issue_item.get("customTypeName", {}),
                    "customStatusName": issue_item.get("customStatusName", {}),
                    "updated": issue_item.get("updated", {}),
                    "default_sheet_name": sheet_val.get("name", "") if has_sheet else "",
                    "default_sheet_number": sheet_val.get("number", "") if has_sheet else "",
                    "web_link":issue_item.get("openLinks").get("web"),
                    "created": issue_item.get("created").get("value"),
                    "priority": issue_item.get("priority", {}).get("value"),
                    "reporter": issue_item.get("reporter", {}).get("value"),
                    "stampAbbr": issue_item.get("stampAbbr", {}).get("value"),
                    "title": issue_item.get("title",{}).get("value"),
                    "deleted_at": issue_item.get("deleted_at",{}.get("value"))
                })
           
            


            print(f"Fetched page {page} of issues.")
        else:
            print(f"Failed to fetch page {page}: {issues_response.text}")
            

    return issue_list 


def get_issues_for_all_projects():

    project_data = fetch_projects()

    access_token = get_valid_access_token()

    project_df = []
    for project in project_data:
        
        isssue_data_list =  fetch_issues(project["project_uuid"],project["project_title"],access_token)

        print(f"Fetching issues for project: {project['project_title']} (UUID: {project['project_uuid']})")
        


        if isssue_data_list:
            project_df.extend(isssue_data_list)

    
    issues_df = pd.DataFrame(project_df)

    save_to_blob_storage(issues_df, 'revizto_issues.csv')

