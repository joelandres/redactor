"""Azure credential shared by the app (Entra ID via DefaultAzureCredential: no keys)."""
from azure.identity.aio import DefaultAzureCredential

credential = DefaultAzureCredential()
