from langchain.agents import create_agent



SYSTEM_PROMPT="""


"""

agent=create_agent(model="google_genai:gemini-3.1-flash-lite",system_prompt=SYSTEM_PROMPT)