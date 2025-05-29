import chromadb
from chromadb.utils import embedding_functions
from transformers import AutoModelForCausalLM, AutoTokenizer
import json
import torch
import logging

# Set up logging
logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

# Initialize ChromaDB client and collection
chroma_client = chromadb.PersistentClient(path="./chroma_db")
collection = chroma_client.get_or_create_collection(
    name="backtest_results",
    embedding_function=embedding_functions.SentenceTransformerEmbeddingFunction(model_name="all-MiniLM-L6-v2")
)

# Load TinyLLaMA locally with custom pad token
model_name = "TinyLLaMA/TinyLLaMA-1.1B-Chat-v1.0"
tokenizer = AutoTokenizer.from_pretrained(model_name)
tokenizer.pad_token = tokenizer.eos_token  # Set pad token to EOS initially
if tokenizer.pad_token == tokenizer.eos_token:
    tokenizer.add_special_tokens({'pad_token': '[PAD]'})  # Add a unique pad token
model = AutoModelForCausalLM.from_pretrained(model_name)
model.resize_token_embeddings(len(tokenizer))  # Resize model embeddings for new token

def query_llm(prompt: str, max_new_tokens: int = 25) -> str:
    """Query TinyLLaMA with a prompt and return the response."""
    logger.debug(f"Prompt length: {len(prompt)} characters")
    inputs = tokenizer(prompt, return_tensors="pt", truncation=True, max_length=512, padding=True)
    input_length = inputs["input_ids"].shape[1]
    logger.debug(f"Input length: {input_length} tokens")

    with torch.no_grad():  # Disable gradient computation for faster inference
        outputs = model.generate(
            inputs["input_ids"],
            attention_mask=inputs["attention_mask"],  # Explicitly provide attention mask
            max_new_tokens=max_new_tokens,
            num_return_sequences=1,
            temperature=0.5,
            top_p=0.9,
            do_sample=True,
            pad_token_id=tokenizer.pad_token_id
        )
    response = tokenizer.decode(outputs[0], skip_special_tokens=True)
    generated_text = response[len(prompt):].strip()
    logger.debug(f"Generated response length: {len(generated_text)} characters")
    return generated_text if generated_text else "No meaningful response generated."

# Query all results and include metadata
results = collection.get(include=["metadatas"])

# Ensure results contain metadata
if not results["metadatas"]:
    print("No backtest results found in ChromaDB.")
else:
    # Convert metadata to a list of dictionaries and limit to recent results
    metadatas = results["metadatas"][:5]  # Limit to 5 most recent backtests
    summarized_metadatas = [
        {k: v for k, v in meta.items() if k in ["symbol", "total_profit", "success_rate", "adx", "volatility"]}
        for meta in metadatas
    ]
    metadata_str = json.dumps(summarized_metadatas, indent=2)
    prompt = (
        "You are a trading strategy analyst. Below is a summarized dataset of the 5 most recent backtest results with "
        "metadata including symbol, total_profit, success_rate, adx, and volatility. Analyze the data to identify "
        "patterns that maximize profits and suggest improvements to the trading strategy parameters (e.g., "
        "stop_loss_pct, target_pct, risk_per_trade). Focus on high-profit scenarios and provide actionable "
        "recommendations in a concise format.\n\n"
        f"Data:\n{metadata_str}\n\n"
        "Analysis and Recommendations:"
    )

    # Query TinyLLaMA
    try:
        response = query_llm(prompt, max_new_tokens=25)
        print("TinyLLaMA Analysis and Recommendations:")
        print(response or "No response generated. Check logs for details.")
    except Exception as e:
        logger.error(f"Error during LLM query: {e}")
        print(f"Error: {e}")