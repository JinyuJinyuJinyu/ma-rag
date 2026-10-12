from azure.core.credentials import AzureKeyCredential
from azure.ai.formrecognizer import DocumentAnalysisClient
from dotenv import load_dotenv
import os
import sys

load_dotenv()

# Default test file from our corpus; pass another PDF path as the first argument.
# On the Free (F0) tier the service analyzes only the first 2 pages of a request.
pdf_path = sys.argv[1] if len(sys.argv) > 1 else "datasets/ai-earnings-desk/NVDA/ir/FY2027-Q2/NVDA_FY2027-Q2_cfo_commentary.pdf"

endpoint = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_ENDPOINT").rstrip('/')
key = os.getenv("AZURE_DOCUMENT_INTELLIGENCE_KEY")

print(f"Testing endpoint: {endpoint}")
print(f"SDK: azure-ai-formrecognizer (stable)")

try:
    client = DocumentAnalysisClient(
        endpoint=endpoint,
        credential=AzureKeyCredential(key)
    )
    
    print("✅ Client created successfully")
    
    # 分析文档
    with open(pdf_path, "rb") as f:
        print(f"📄 Analyzing {pdf_path}...")
        poller = client.begin_analyze_document("prebuilt-layout", document=f)
        
        print("⏳ Waiting for analysis...")
        result = poller.result()
    
    print("✅ Success!")
    print(f"📊 Pages: {len(result.pages)}")
    
    # 提取一些文本
    if result.content:
        print(f"📝 Text length: {len(result.content)} characters")
        print(f"📝 First 200 chars: {result.content[:200]}...")
    
except Exception as e:
    print(f"❌ Error: {e}")
    print(f"Error type: {type(e).__name__}")