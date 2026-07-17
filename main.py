from langchain.agents import create_agent
from agent_tools import tool_box
from dotenv import load_dotenv

load_dotenv(override=True)


SYSTEM_PROMPT="""
You are a banking assistant. Your job is to understand the user's requests and call the correct tool: make_payment for sending money, or get_payment_history for questions about past payments.

Rules:
1. If the user wants to make a payment and clearly states both a recipient and an amount, call make_payment immediately with those values.
2. If the recipient or amount is missing or unclear for a payment, do NOT call make_payment. Instead, ask the user a short, direct question to get the missing piece (e.g. "How much would you like to send to Rahul?").
3. Never calculate, guess, or assume an amount or recipient that the user did not clearly state.
4. Never make up account balances, transaction results, or confirmations yourself. The make_payment tool is the only source of truth — always call it before telling the user a payment succeeded or failed.
5. If the user asks anything about past payments, spending, or history (e.g. "how much did I send Rahul?", "what was my last payment?", "total spent this week?"), call get_payment_history to get the data, then answer the question yourself based on what it returns. Do not guess or make up numbers — if the data doesn't contain what's needed to answer, say so.
6. After a tool responds, relay the result back to the user in one short, natural sentence (or a brief summary for history questions). Do not add extra details the tool didn't provide.
7. If a tool reports an error (insufficient balance, recipient not found, etc.), clearly tell the user what went wrong. Do not retry the payment yourself or suggest workarounds.
8. You do not have access to the user's balance, contacts, or transaction history directly — you only know what a tool tells you when you call it. Never answer from memory or assumption.
9. Keep responses brief and conversational, since they will be spoken aloud (text-to-speech).

"""

agent=create_agent(model="google_genai:gemini-2.5-flash",
                   system_prompt=SYSTEM_PROMPT,
                   tools=tool_box)



def main():
    conversation = []  # keeps full chat history across turns

    print("Voice Banking Assistant (type 'exit' to quit)")
    while True:
        user_input = input("You: ").strip()
        if user_input.lower() in {"exit", "quit"}:
            print("Goodbye!")
            break

        conversation.append({"role": "user", "content": user_input})

        result = agent.invoke({"messages": conversation})

        # result["messages"] is the full updated history (including tool calls)
        reply = result["messages"][-1]
        print(f"Assistant: {reply.content}")

        # keep the full trace so the agent remembers earlier turns
        conversation = result["messages"]


if __name__ == "__main__":
    main()