---
rule_kind: editFormRule
intent: block editing once the form is marked as Reviewed
entity_param: individual
encounter_types: []
concepts: []
source_org: Goonj
---
```js
"use strict";
 ({params, imports}) => {
  const { entity, form, services, entityContext, myUserGroups, userInfo } = params;
  const individual = params.entity;
 

  const output = {};
  
  const condition11 = new imports.rulesConfig.RuleCondition({individual}).when.valueInRegistration("d0557b1d-4e21-4111-8b61-8f7256c845e8").containsAnswerConceptName("303d5ad1-ffa2-4096-ba7c-a86876ca9d12").matches();
 

 

  if (condition11) {
  output.eligible = {
  value: false,
  message: "Edit access denied: Form has been marked as Reviewed."
  };
  }
 

  return output;
 };
```
