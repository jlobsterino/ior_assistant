import asyncio
import os
import sys

# Set stdout/stderr encoding to utf-8
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

# Add project root to python path
sys.path.append(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

# Set env variables
os.environ["APP_ENV"] = "local"
os.environ["MOCK_NOTEBOOK_EXECUTION"] = "true"

from backend.agent.agent_flow import run_agent

async def main():
    session_id = "test_session_hypothesis_direct"
    user_message = "Выведи гипотезу по всем ИОРам за Q1 2025 года"
    
    print(f"Running agent with query: {user_message}")
    print("-" * 50)
    
    try:
        async for sse_event in run_agent(session_id=session_id, user_message=user_message):
            # Print representational string to avoid terminal encoding issues
            print(repr(sse_event))
    except Exception as e:
        print("CRITICAL EXCEPTION IN FLOW:")
        import traceback
        traceback.print_exc()

if __name__ == "__main__":
    asyncio.run(main())
