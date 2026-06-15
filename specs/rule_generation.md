schedule a delivery visit after 10 days if risk of high


- context - from where
- return format
- what visit types are there - bundle - srs
- what field has value
- how to get the value from field - many predefined

2 types:
- RAG - vectorise and store - chunks independent
  - store in json - dont chunk - cant just store
    - vectorise only when need to run similiarty search
  - knowledge graph
  - croma or from meta - store method and description of methods
    - dont chunk - 
    - embedding
    - clear when to use what
- agentic:
  - slower
- query structured tables
- maps to sql query based on how question asked
  - directly invoke vector db - fastest than llm
- ask for plan

data: input, output format, methods, history(no need so many examples) - fewshot enough

generate rule
