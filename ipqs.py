# Install and Import dependencies
!pip install -q "openai>=1.0.0" sentence-transformers faiss-cpu PyPDF2 python-docx gradio

import os, json, re, time, uuid, tempfile, shutil, traceback
from collections import deque
from typing import List, Dict, Any, Optional

import gradio as gr
from openai import OpenAI
from sentence_transformers import SentenceTransformer
import faiss
from PyPDF2 import PdfReader
from docx import Document

# ===== CONFIGURATION =====
# Set your OpenAI API KEY here - IMPORTANT!
OPENAI_API_KEY = "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"  # ⚠️ REPLACE WITH YOUR ACTUAL API KEY

# Initialize components with error handling
client = None
EMBED_MODEL = None

def initialize_services():
    global client, EMBED_MODEL

    # Initialize OpenAI
    if OPENAI_API_KEY and OPENAI_API_KEY != "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":
        try:
            client = OpenAI(api_key=OPENAI_API_KEY)
            # Test the connection
            client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": "test"}],
                max_tokens=1
            )
            print("✅ OpenAI client initialized and tested")
        except Exception as e:
            print(f"⚠️ OpenAI client error: {e}")
            client = None
    else:
        print("⚠️ OpenAI API key not set")

    # Initialize embedding model
    try:
        EMBED_MODEL = SentenceTransformer('all-MiniLM-L6-v2')
        print("✅ Embedding model loaded")
    except Exception as e:
        print(f"❌ Embedding model error: {e}")
        EMBED_MODEL = None

# Initialize services
initialize_services()

# ===== GLOBAL VARIABLES =====
doc_chunks: List[str] = []
chunk_metadata: List[dict] = []
faiss_index = None
history = deque(maxlen=50)

def reset_all():
    """Resets all indices and stores."""
    global doc_chunks, chunk_metadata, faiss_index, history
    doc_chunks.clear()
    chunk_metadata.clear()
    faiss_index = None
    history.clear()
    return create_success_message("🔄 System reset successfully!")

def create_success_message(text):
    return f"""
    <div style="background: linear-gradient(135deg, #4CAF50, #45a049); color: white; padding: 15px;
                border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin: 10px 0;">
        <i class="fas fa-check-circle"></i> {text}
    </div>
    """

def create_error_message(text):
    return f"""
    <div style="background: linear-gradient(135deg, #f44336, #d32f2f); color: white; padding: 15px;
                border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin: 10px 0;">
        <i class="fas fa-exclamation-triangle"></i> {text}
    </div>
    """

def create_warning_message(text):
    return f"""
    <div style="background: linear-gradient(135deg, #ff9800, #f57c00); color: white; padding: 15px;
                border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin: 10px 0;">
        <i class="fas fa-exclamation-circle"></i> {text}
    </div>
    """

def create_info_message(text):
    return f"""
    <div style="background: linear-gradient(135deg, #2196F3, #1976D2); color: white; padding: 15px;
                border-radius: 10px; box-shadow: 0 4px 6px rgba(0,0,0,0.1); margin: 10px 0;">
        <i class="fas fa-info-circle"></i> {text}
    </div>
    """

def parse_pdf(file_path: str) -> List[dict]:
    """Extract chunks from PDF file with error handling."""
    try:
        reader = PdfReader(file_path)
        chunks = []

        if len(reader.pages) == 0:
            return []

        for page_num, page in enumerate(reader.pages):
            try:
                text = page.extract_text()
                if text and text.strip():
                    # Split by paragraphs
                    paragraphs = text.split('\n\n')
                    for para_idx, para in enumerate(paragraphs):
                        if para.strip() and len(para.strip()) > 30:
                            chunks.append({
                                "text": para.strip(),
                                "source": f"Page {page_num+1}",
                                "document": os.path.basename(file_path),
                                "type": "PDF",
                                "page_num": page_num + 1
                            })
            except Exception as e:
                print(f"Error processing page {page_num+1}: {e}")
                continue

        return chunks
    except Exception as e:
        print(f"Error parsing PDF {file_path}: {e}")
        return []

def parse_docx(file_path: str) -> List[dict]:
    """Extract chunks from DOCX file with error handling."""
    try:
        doc = Document(file_path)
        chunks = []

        for para_idx, para in enumerate(doc.paragraphs):
            if para.text and para.text.strip() and len(para.text.strip()) > 30:
                chunks.append({
                    "text": para.text.strip(),
                    "source": f"Paragraph {para_idx+1}",
                    "document": os.path.basename(file_path),
                    "type": "DOCX",
                    "para_num": para_idx + 1
                })

        return chunks
    except Exception as e:
        print(f"Error parsing DOCX {file_path}: {e}")
        return []

def ingest_documents(files: List) -> tuple:
    """Process files with comprehensive error handling."""
    global doc_chunks, chunk_metadata, faiss_index, EMBED_MODEL

    if not files:
        return 0, create_warning_message("No files provided")

    if EMBED_MODEL is None:
        return 0, create_error_message("Embedding model not loaded. Please refresh and try again.")

    try:
        _all_chunks, _all_meta = [], []
        processed_files = []

        for file in files:
            if not hasattr(file, 'name'):
                continue

            file_path = file.name
            ext = file_path.split('.')[-1].lower()

            print(f"Processing {file_path} (type: {ext})")

            if ext == 'pdf':
                chunks = parse_pdf(file_path)
            elif ext in ['docx', 'doc']:
                chunks = parse_docx(file_path)
            else:
                print(f"Unsupported file type: {ext}")
                continue

            if chunks:
                _all_chunks.extend([c["text"] for c in chunks])
                _all_meta.extend(chunks)
                processed_files.append(os.path.basename(file_path))

        if not _all_chunks:
            return 0, create_warning_message("No text content extracted from uploaded files. Please check if files contain readable text.")

        # Generate embeddings
        print(f"Generating embeddings for {len(_all_chunks)} chunks...")
        embeddings = EMBED_MODEL.encode(_all_chunks, show_progress_bar=True)

        # Update global stores
        start_idx = len(doc_chunks)
        doc_chunks.extend(_all_chunks)
        chunk_metadata.extend(_all_meta)

        # Update FAISS index
        if faiss_index is None:
            faiss_index = faiss.IndexFlatL2(embeddings.shape[1])
            print(f"Created new FAISS index with dimension {embeddings.shape[1]}")

        faiss_index.add(embeddings.astype('float32'))

        success_msg = create_success_message(
            f"Successfully processed {len(processed_files)} files: {', '.join(processed_files)}<br>"
            f"📄 Extracted {len(_all_chunks)} text chunks<br>"
            f"🔍 Vector index updated with {faiss_index.ntotal} total chunks"
        )
        print(f"✅ Success: {len(_all_chunks)} chunks processed")
        return len(_all_chunks), success_msg

    except Exception as e:
        error_msg = create_error_message(f"Error in document ingestion: {str(e)}")
        print(f"❌ Error: {e}")
        print(traceback.format_exc())
        return 0, error_msg

def parse_query_with_fallback(query_text: str) -> dict:
    """Parse query with LLM or fallback to rule-based parsing."""

    # Try LLM parsing first
    if client:
        try:
            prompt = f"""
Extract information from this query and return as JSON:
- age (number)
- gender (string)
- procedure (string)
- location (string)
- policy_duration_months (number)

Query: "{query_text}"

Return only valid JSON. Example: {{"age": 46, "procedure": "knee surgery"}}
"""

            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0,
                max_tokens=200
            )

            content = response.choices[0].message.content.strip()

            # Clean JSON
            if content.startswith('```'):
                parts = content.split('```')
                if len(parts) >= 2:
                    content = parts[1]
                    if content.startswith('json'):
                        content = content[4:]

            result = json.loads(content)
            return result

        except Exception as e:
            print(f"LLM parsing failed: {e}")
            # Fall through to rule-based parsing

    # Rule-based fallback parsing
    result = {"query": query_text}
    query_lower = query_text.lower()

    # Extract age
    age_match = re.search(r'(\d+)[-\s]*(?:year|yr|y)[-\s]*old|(\d+)m\b', query_lower)
    if age_match:
        result['age'] = int(age_match.group(1) or age_match.group(2))

    # Extract gender
    if 'male' in query_lower and 'female' not in query_lower:
        result['gender'] = 'male'
    elif 'female' in query_lower:
        result['gender'] = 'female'

    # Extract procedures
    procedures = ['surgery', 'treatment', 'therapy', 'consultation', 'procedure', 'operation']
    for proc in procedures:
        if proc in query_lower:
            # Get context around the procedure
            idx = query_lower.find(proc)
            start = max(0, idx - 15)
            end = min(len(query_text), idx + 15)
            result['procedure'] = query_text[start:end].strip()
            break

    # Extract locations
    cities = ['mumbai', 'delhi', 'bangalore', 'pune', 'chennai', 'kolkata', 'hyderabad']
    for city in cities:
        if city in query_lower:
            result['location'] = city.capitalize()
            break

    # Extract duration
    month_match = re.search(r'(\d+)[-\s]*month', query_lower)
    year_match = re.search(r'(\d+)[-\s]*year', query_lower)
    if month_match:
        result['policy_duration_months'] = int(month_match.group(1))
    elif year_match:
        result['policy_duration_months'] = int(year_match.group(1)) * 12

    return result

def semantic_search(query_struct: dict, top_k: int = 5) -> List[int]:
    """Perform semantic search with error handling."""
    global doc_chunks, faiss_index, EMBED_MODEL

    if faiss_index is None or not doc_chunks or EMBED_MODEL is None:
        return []

    try:
        # Create search query
        query_parts = []
        for key, value in query_struct.items():
            if value and key not in ["error", "query"]:
                query_parts.append(str(value))

        query_text = " ".join(query_parts) if query_parts else query_struct.get("query", "insurance policy coverage")
        print(f"🔍 Search query: {query_text}")

        # Generate embedding and search
        query_embedding = EMBED_MODEL.encode([query_text])
        distances, indices = faiss_index.search(query_embedding, min(top_k, len(doc_chunks)))

        valid_indices = [idx for idx in indices[0] if 0 <= idx < len(doc_chunks)]
        return valid_indices

    except Exception as e:
        print(f"Error in semantic search: {e}")
        return []

def generate_rule_based_answer(query_struct: dict, relevant_chunks: List[str], metadatas: List[dict]) -> dict:
    """Generate a simple rule-based answer when LLM is not available."""

    if not relevant_chunks:
        return {
            "answer": "No relevant information found in uploaded documents",
            "justification": "Please upload relevant policy documents and try again",
            "clauses_cited": []
        }

    # Simple keyword matching for common insurance queries
    query_text = query_struct.get("query", "").lower()
    procedure = query_struct.get("procedure", "").lower()

    # Basic coverage determination
    coverage_keywords = ['cover', 'coverage', 'covered', 'include', 'benefit', 'eligible']
    exclusion_keywords = ['exclude', 'not covered', 'limitation', 'exception']

    answer = "Based on the uploaded documents, "
    justification = ""

    # Check if any chunk contains coverage information
    for i, chunk in enumerate(relevant_chunks[:3]):
        chunk_lower = chunk.lower()
        if any(keyword in chunk_lower for keyword in coverage_keywords):
            if procedure and procedure in chunk_lower:
                answer += f"the requested procedure appears to be covered. "
            else:
                answer += f"coverage information is available. "

            justification += f"Clause {i+1} from {metadatas[i].get('document', 'document')} contains relevant coverage information. "

        if any(keyword in chunk_lower for keyword in exclusion_keywords):
            justification += f"Clause {i+1} also mentions exclusions or limitations. "

    if not justification:
        answer += "please review the relevant clauses below for specific coverage details."
        justification = "The system found relevant policy clauses but could not determine specific coverage automatically."

    return {
        "answer": answer,
        "justification": justification,
        "clauses_cited": [f"Clause {i+1}" for i in range(min(3, len(relevant_chunks)))]
    }

def answer_query_with_fallback(query_struct: dict, relevant_chunks: List[str], metadatas: List[dict], question: str) -> dict:
    """Generate answer with LLM or fallback to rule-based approach."""

    if not relevant_chunks:
        return {
            "answer": "No relevant information found in uploaded documents",
            "justification": "Please upload relevant policy documents first",
            "clauses_cited": []
        }

    # Try LLM approach first
    if client:
        try:
            # Prepare context
            context_parts = []
            for i, (chunk, meta) in enumerate(zip(relevant_chunks[:5], metadatas[:5])):
                source = meta.get('source', 'Unknown')
                doc_name = meta.get('document', 'Unknown')
                context_parts.append(f"[Clause {i+1} from {doc_name}, {source}]: {chunk[:400]}")

            context = "\n\n".join(context_parts)

            prompt = f"""
Based on the policy clauses below, answer the question directly and clearly.

DOCUMENT CLAUSES:
{context}

QUESTION: {question}

Provide a JSON response with:
- "answer": Direct answer to the question
- "justification": Brief explanation with clause references
- "clauses_cited": List of clause numbers used

Keep the response concise and specific.
"""

            response = client.chat.completions.create(
                model="gpt-3.5-turbo",
                messages=[{"role": "user", "content": prompt}],
                temperature=0.1,
                max_tokens=500
            )

            content = response.choices[0].message.content.strip()

            # Clean and parse JSON
            if content.startswith('```'):
                parts = content.split('```')
                if len(parts) >= 2:
                    content = parts[1]
                    if content.startswith('json'):
                        content = content[4:]

            result = json.loads(content)
            return result

        except Exception as e:
            print(f"LLM answer generation failed: {e}")
            # Fall through to rule-based approach

    # Rule-based fallback
    print("Using rule-based answer generation")
    return generate_rule_based_answer(query_struct, relevant_chunks, metadatas)

def process_query(files, query):
    """Main processing pipeline with comprehensive error handling and attractive UI."""
    try:
        if not query or not query.strip():
            return create_warning_message("Please enter a query to get started! 💬")

        # Process uploaded files if any
        status_messages = []
        if files:
            num_chunks, ingest_msg = ingest_documents(files)
            status_messages.append(ingest_msg)

        # Check if we have any documents
        if not doc_chunks:
            return create_warning_message(
                "📄 No documents available.<br>"
                "Please upload PDF or DOCX files first to analyze them."
            )

        # Parse query
        print(f"📝 Processing query: {query}")
        query_struct = parse_query_with_fallback(query)

        # Search for relevant clauses
        clause_indices = semantic_search(query_struct, top_k=6)

        if not clause_indices:
            return create_warning_message(
                "🔍 No relevant clauses found.<br>"
                "Try rephrasing your query or upload more relevant documents."
            )

        # Get relevant chunks and metadata
        relevant_chunks = [doc_chunks[i] for i in clause_indices]
        relevant_metadata = [chunk_metadata[i] for i in clause_indices]

        # Generate answer
        answer_result = answer_query_with_fallback(query_struct, relevant_chunks, relevant_metadata, query)

        # Create beautiful HTML output
        html_output = ""

        # Status messages
        for msg in status_messages:
            html_output += msg

        # Query Analysis Section
        html_output += f"""
        <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                    color: white; padding: 20px; border-radius: 15px; margin: 15px 0;
                    box-shadow: 0 8px 20px rgba(0,0,0,0.15);">
            <h2 style="margin-top: 0; display: flex; align-items: center;">
                🔍 <span style="margin-left: 10px;">Query Analysis</span>
            </h2>
            <div style="background: rgba(255,255,255,0.1); padding: 15px; border-radius: 10px;
                        backdrop-filter: blur(10px);">
                <h4>📊 Extracted Information:</h4>
                <pre style="background: rgba(0,0,0,0.2); padding: 12px; border-radius: 8px;
                           font-family: 'Courier New', monospace; color: #fff; overflow-x: auto;">
{json.dumps(query_struct, indent=2)}</pre>
            </div>
        </div>
        """

        # Relevant Clauses Section
        html_output += f"""
        <div style="background: linear-gradient(135deg, #f093fb 0%, #f5576c 100%);
                    color: white; padding: 20px; border-radius: 15px; margin: 15px 0;
                    box-shadow: 0 8px 20px rgba(0,0,0,0.15);">
            <h2 style="margin-top: 0; display: flex; align-items: center;">
                📋 <span style="margin-left: 10px;">Relevant Policy Clauses</span>
            </h2>
        """

        for i, (chunk, meta) in enumerate(zip(relevant_chunks, relevant_metadata)):
            doc_icon = "📄" if meta.get('type') == 'PDF' else "📝"
            html_output += f"""
            <div style="background: rgba(255,255,255,0.15); margin: 10px 0; padding: 15px;
                        border-radius: 12px; backdrop-filter: blur(10px); border: 1px solid rgba(255,255,255,0.2);">
                <div style="display: flex; align-items: center; margin-bottom: 8px;">
                    <span style="font-size: 1.2em;">{doc_icon}</span>
                    <strong style="margin-left: 8px;">Clause {i+1}</strong>
                    <span style="margin-left: auto; background: rgba(255,255,255,0.2);
                                padding: 4px 8px; border-radius: 15px; font-size: 0.85em;">
                        {meta.get('document', 'Unknown')} • {meta.get('source', 'Unknown')}
                    </span>
                </div>
                <p style="margin: 0; line-height: 1.6; background: rgba(0,0,0,0.1);
                          padding: 12px; border-radius: 8px; font-size: 0.95em;">
                    {chunk[:400]}{'...' if len(chunk) > 400 else ''}
                </p>
            </div>
            """

        html_output += "</div>"

        # Answer Section
        html_output += f"""
        <div style="background: linear-gradient(135deg, #4facfe 0%, #00f2fe 100%);
                    color: white; padding: 20px; border-radius: 15px; margin: 15px 0;
                    box-shadow: 0 8px 20px rgba(0,0,0,0.15);">
            <h2 style="margin-top: 0; display: flex; align-items: center;">
                💡 <span style="margin-left: 10px;">AI-Generated Response</span>
            </h2>

            <div style="background: rgba(255,255,255,0.15); padding: 18px; border-radius: 12px;
                        backdrop-filter: blur(10px); margin-bottom: 15px;">
                <h3 style="margin-top: 0; color: #fff;">📝 Answer:</h3>
                <p style="font-size: 1.1em; line-height: 1.7; margin: 0; font-weight: 400;">
                    {answer_result.get('answer', 'No answer generated')}
                </p>
            </div>

            <div style="background: rgba(255,255,255,0.15); padding: 18px; border-radius: 12px;
                        backdrop-filter: blur(10px); margin-bottom: 15px;">
                <h3 style="margin-top: 0; color: #fff;">🔍 Justification:</h3>
                <p style="line-height: 1.6; margin: 0;">
                    {answer_result.get('justification', 'No justification provided')}
                </p>
            </div>

            <div style="background: rgba(255,255,255,0.15); padding: 18px; border-radius: 12px;
                        backdrop-filter: blur(10px);">
                <h3 style="margin-top: 0; color: #fff;">📚 Referenced Clauses:</h3>
                <p style="margin: 0;">
                    {', '.join(answer_result.get('clauses_cited', ['None']))}
                </p>
            </div>
        </div>
        """

        # JSON Output Section
        html_output += f"""
        <div style="background: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%);
                    color: #333; padding: 20px; border-radius: 15px; margin: 15px 0;
                    box-shadow: 0 8px 20px rgba(0,0,0,0.15);">
            <h2 style="margin-top: 0; display: flex; align-items: center; color: #333;">
                📊 <span style="margin-left: 10px;">Structured JSON Output</span>
            </h2>
            <pre style="background: #2d3748; color: #e2e8f0; padding: 20px; border-radius: 12px;
                       overflow-x: auto; font-family: 'Monaco', 'Consolas', monospace;
                       box-shadow: inset 0 2px 4px rgba(0,0,0,0.2); line-height: 1.5;">
{json.dumps(answer_result, indent=2, ensure_ascii=False)}</pre>
        </div>
        """

        # Add to history
        history.appendleft({
            "query": query,
            "entities": query_struct,
            "answer": answer_result,
            "timestamp": time.strftime("%H:%M:%S")
        })

        return html_output

    except Exception as e:
        error_msg = f"❌ Unexpected error: {str(e)}\n\n🔍 Debug info:\n{traceback.format_exc()}"
        print(error_msg)
        return create_error_message(f"System Error<br><pre style='font-size: 0.85em;'>{error_msg}</pre>")

def show_history():
    """Display query history with attractive styling."""
    if not history:
        return create_info_message("No queries processed yet. Start by uploading documents and asking questions! 🚀")

    html = f"""
    <div style="background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%);
                padding: 20px; border-radius: 15px; box-shadow: 0 8px 20px rgba(0,0,0,0.15);">
        <h3 style="margin-top: 0; color: #8b4513;">📈 Recent Query History</h3>
        <div style="max-height: 400px; overflow-y: auto;">
    """

    for i, entry in enumerate(list(history)[:10]):
        html += f"""
        <div style="background: rgba(255,255,255,0.8); padding: 15px; margin: 8px 0;
                    border-radius: 10px; border-left: 4px solid #ff6b6b;
                    box-shadow: 0 2px 4px rgba(0,0,0,0.1);">
            <div style="display: flex; justify-content: between; align-items: center; margin-bottom: 8px;">
                <strong style="color: #2c3e50;">Query #{len(history)-i}</strong>
                <span style="background: #34495e; color: white; padding: 2px 8px;
                            border-radius: 12px; font-size: 0.8em;">
                    {entry.get('timestamp', 'Unknown time')}
                </span>
            </div>
            <div style="font-style: italic; color: #555; margin-bottom: 8px;">
                "{entry['query']}"
            </div>
            <div style="font-size: 0.9em; color: #666; background: #f8f9fa;
                        padding: 8px; border-radius: 6px;">
                <strong>Answer:</strong> {entry['answer'].get('answer', 'No answer')[:100]}...
            </div>
        </div>
        """

    html += "</div></div>"
    return html

def show_system_status():
    """Show current system status with attractive styling."""

    # Check system health
    openai_status = "🟢 Connected" if client else "🔴 Not Connected"
    embedding_status = "🟢 Loaded" if EMBED_MODEL else "🔴 Not Loaded"
    index_status = "🟢 Ready" if faiss_index else "🟡 Not Initialized"

    unique_docs = len(set(meta.get('document', '') for meta in chunk_metadata))

    status_html = f"""
    <div style="background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white; padding: 20px; border-radius: 15px;
                box-shadow: 0 8px 20px rgba(0,0,0,0.15);">
        <h3 style="margin-top: 0; display: flex; align-items: center;">
            📊 <span style="margin-left: 10px;">System Status</span>
        </h3>

        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr));
                    gap: 15px; margin-top: 15px;">

            <div style="background: rgba(255,255,255,0.15); padding: 15px; border-radius: 10px;
                        backdrop-filter: blur(10px);">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 5px;">
                    📄 Documents
                </div>
                <div style="font-size: 1.5em; font-weight: bold; color: #4CAF50;">
                    {unique_docs}
                </div>
                <div style="font-size: 0.9em; opacity: 0.8;">
                    Indexed
                </div>
            </div>

            <div style="background: rgba(255,255,255,0.15); padding: 15px; border-radius: 10px;
                        backdrop-filter: blur(10px);">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 5px;">
                    🔤 Text Chunks
                </div>
                <div style="font-size: 1.5em; font-weight: bold; color: #2196F3;">
                    {len(doc_chunks)}
                </div>
                <div style="font-size: 0.9em; opacity: 0.8;">
                    Available
                </div>
            </div>

            <div style="background: rgba(255,255,255,0.15); padding: 15px; border-radius: 10px;
                        backdrop-filter: blur(10px);">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 5px;">
                    🤖 OpenAI API
                </div>
                <div style="font-size: 1.2em; font-weight: bold;">
                    {openai_status}
                </div>
            </div>

            <div style="background: rgba(255,255,255,0.15); padding: 15px; border-radius: 10px;
                        backdrop-filter: blur(10px);">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 5px;">
                    🧠 Embeddings
                </div>
                <div style="font-size: 1.2em; font-weight: bold;">
                    {embedding_status}
                </div>
            </div>

            <div style="background: rgba(255,255,255,0.15); padding: 15px; border-radius: 10px;
                        backdrop-filter: blur(10px);">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 5px;">
                    🔍 Search Index
                </div>
                <div style="font-size: 1.2em; font-weight: bold;">
                    {index_status}
                </div>
            </div>

            <div style="background: rgba(255,255,255,0.15); padding: 15px; border-radius: 10px;
                        backdrop-filter: blur(10px);">
                <div style="font-size: 1.1em; font-weight: bold; margin-bottom: 5px;">
                    📝 Queries
                </div>
                <div style="font-size: 1.5em; font-weight: bold; color: #FF9800;">
                    {len(history)}
                </div>
                <div style="font-size: 0.9em; opacity: 0.8;">
                    Processed
                </div>
            </div>
        </div>
    </div>
    """
    return status_html

# ===== GRADIO INTERFACE WITH ATTRACTIVE THEME =====
custom_css = """
.gradio-container {
    font-family: 'Segoe UI', Tahoma, Geneva, Verdana, sans-serif !important;
}

.gr-button {
    background: linear-gradient(135deg, #667eea 0%, #764ba2 100%) !important;
    border: none !important;
    border-radius: 25px !important;
    padding: 12px 24px !important;
    font-weight: 600 !important;
    color: white !important;
    box-shadow: 0 4px 15px rgba(0,0,0,0.2) !important;
    transition: all 0.3s ease !important;
}

.gr-button:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 6px 20px rgba(0,0,0,0.3) !important;
}

.gr-textbox textarea, .gr-textbox input {
    border-radius: 15px !important;
    border: 2px solid #e1e5e9 !important;
    padding: 15px !important;
}

.gr-file {
    border-radius: 15px !important;
    border: 2px dashed #667eea !important;
    background: linear-gradient(135deg, #f8f9ff 0%, #f0f2ff 100%) !important;
}
"""

with gr.Blocks(title="🧠 LLM-Powered Policy QA System", theme="soft", css=custom_css) as demo:
    gr.Markdown("""
    <div style="text-align: center; background: linear-gradient(135deg, #667eea 0%, #764ba2 100%);
                color: white; padding: 30px; border-radius: 20px; margin-bottom: 20px;
                box-shadow: 0 10px 30px rgba(0,0,0,0.2);">
        <h1 style="font-size: 2.5em; margin: 0; text-shadow: 2px 2px 4px rgba(0,0,0,0.3);">
            🧠 Intelligent Policy Query System
        </h1>
        <p style="font-size: 1.2em; margin: 10px 0 0 0; opacity: 0.9;">
            HackRx 6.0 Solution • LLM-Powered Document Analysis with Clause-Level Citations
        </p>
    </div>

    <div style="background: linear-gradient(135deg, #a8edea 0%, #fed6e3 100%);
                padding: 20px; border-radius: 15px; margin-bottom: 20px; color: #333;">
        <p style="margin: 0; font-size: 1.1em; text-align: center;">
            📄 Upload policy documents (PDF/DOCX) and ask natural language questions to get precise,
            explainable answers with clause-level citations and structured JSON output.
        </p>
    </div>
    """)

    with gr.Row():
        with gr.Column(scale=2):
            files = gr.Files(
                label="📄 Upload Policy Documents (PDF/DOCX)",
                file_types=['.pdf', '.docx'],
                file_count="multiple",
                height=120
            )

            query_input = gr.Textbox(
                label="💬 Enter Your Query",
                placeholder="Example: Does this policy cover knee surgery for a 46-year-old male with a 3-month old policy?",
                lines=3,
                max_lines=5
            )

            with gr.Row():
                process_btn = gr.Button("🔍 Process Query", variant="primary", size="lg")
                reset_btn = gr.Button("🔄 Reset System", variant="secondary", size="lg")

        with gr.Column(scale=1):
            status_display = gr.HTML(label="📊 System Status")

    # Main output area
    result_output = gr.HTML(label="📋 Analysis Results")

    # Collapsible sections
    with gr.Accordion("📈 Query History", open=False):
        history_output = gr.HTML()

    # Event handlers
    process_btn.click(
        fn=process_query,
        inputs=[files, query_input],
        outputs=[result_output]
    )

    reset_btn.click(
        fn=reset_all,
        outputs=[result_output]
    )

    process_btn.click(
        fn=show_history,
        outputs=[history_output]
    )

    # Load status on startup
    demo.load(
        fn=show_system_status,
        outputs=[status_display]
    )

    # Footer
    gr.Markdown("""
    <div style="background: linear-gradient(135deg, #ffecd2 0%, #fcb69f 100%);
                padding: 20px; border-radius: 15px; margin-top: 20px; text-align: center;">
        <h3 style="margin-top: 0;">💡 Key Features</h3>
        <div style="display: grid; grid-template-columns: repeat(auto-fit, minmax(200px, 1fr)); gap: 15px;">
            <div>🗂️ <strong>Multi-format Support</strong><br>PDF and DOCX documents</div>
            <div>🔍 <strong>Semantic Search</strong><br>FAISS-powered retrieval</div>
            <div>🤖 <strong>LLM Reasoning</strong><br>GPT-powered analysis</div>
            <div>📊 <strong>Structured Output</strong><br>Machine-readable JSON</div>
            <div>🔗 <strong>Clause Citations</strong><br>Explainable decisions</div>
            <div>📝 <strong>Query History</strong><br>Session tracking</div>
        </div>

        <p style="margin-top: 20px; font-size: 0.9em; opacity: 0.8;">
            <strong>Tech Stack:</strong> OpenAI GPT-3.5 • Sentence Transformers • FAISS • Gradio • PyPDF2
        </p>
    </div>
    """)

# Launch the interface
print("🚀 Starting enhanced Gradio interface...")
demo.launch(share=True, debug=True, server_name="0.0.0.0")
