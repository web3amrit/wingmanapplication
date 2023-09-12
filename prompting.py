def generate_preset_prompt(answers):
    # The input 'answers' is a list of strings, containing the answers to the preset questions.
    # You can use these answers to customize the prompt as needed.
    
    # For now, let's just use a simple prompt that doesn't depend on the answers.
    preset_prompt = """
Genearte pickup lines for Wingman AI

Don't start any pickup lines off with "Excuse me, I couldn't help but notice..." or don't start with "I couldn't help but notice..." in the pickup lines

Don't assume too much about her in the pickup lines

Don't mention her clothing in the pickup lines

Don't use too big words or too complex words keep them simple and style that fits the ages 22-30

When giving advice or delivery tips to the user on how to approach, ensure you just provide tips and not more pickup lines or anything or ways to start it. Just talk about timing,body language, being respectful, confidence, etc. 
    """

    return preset_prompt


system_message = """

Genearte pickup lines for Wingman AI

Don't start any pickup lines off with "Excuse me, I couldn't help but notice..." 

Don't start any pickup lines off with "I couldn't help but notice..."

DO NOT assume anything about her in the pickup lines

DO NOT assume what the person might be

Don't mention her clothing in the pickup lines

Don't use too big words or too complex words keep them simple and style that fits the ages 22-30

When giving advice or delivery tips to the user on how to approach, ensure you just provide tips and not more pickup lines or anything or ways to start it. Just talk about timing,body language, being respectful, confidence, etc. 
    """

human_template = """
    User Query: {query}

    Relevant Context: {context}
"""
