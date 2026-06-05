- create a spec for generating visit schedule rule
- documentation: https://avni.readme.io/docs/writing-rules#4-visit-schedule-rule
- in the modelling document should be able to parse scheduling rule column or later change rule by interaction in chat
- Based on input rule will change - on how to access the input
- Rule can use inbuilt methods to generate the function: https://avni.readme.io/docs/helper-functions
- In the Self-service improvement.xlsx file - 'VS rule' tab - we can see several examples
- The approach should be extensible for other rules here: https://avni.readme.io/docs/writing-rules

Feedback:
- Finally automatable is for a different purpose - can consider all rules in the 'VS rule' tab
- there are all these helper functions that might be needed - https://avni.readme.io/docs/helper-functions but I see only few categories mentioned
- vectorise only when a query is needed - meaning?
- does it help to give so many examples - or without storing few_shot better?
- instead of dropping on invalid JS - can we retry? - later implement - so not to overengineer
- better to generate_rules after form_mappings are generated because than will have the subject type and enrolment context?


- 
- prototype -> fresh org -> upload same bundle - we get to know high level in scoping phase - using dropdown done currently 
  - complex rules are skipped because cant do via dropdown
- if not structured then new org - exact due dates we get to know later
