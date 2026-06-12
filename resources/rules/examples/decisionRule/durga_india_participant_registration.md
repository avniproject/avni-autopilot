---
rule_kind: decisionRule
intent: record the user who entered the registration as an audit decision
entity_param: individual
encounter_types: []
concepts: []
source_org: Durga India
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const decisions = params.decisions;
  const moment = imports.moment;
  const registrationDecisions = [];
  
  let createdByName = params.user.name;
  let createdByUsername = params.user.username;
  
  if(createdByName){
  registrationDecisions.push({name: "Created By Name", value: createdByName});  
  }
  
  if(createdByUsername){
  registrationDecisions.push({name: "Created By Username", value: createdByUsername});  
  }
  
  decisions.registrationDecisions.push(...registrationDecisions);
  
  return decisions;
 };
```
