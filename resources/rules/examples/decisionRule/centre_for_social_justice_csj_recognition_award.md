---
rule_kind: decisionRule
intent: record creator name and username on every save, and stamp the registration
  date only on the first save
entity_param: individual
encounter_types: []
concepts:
- 7b1a1540-bfd5-484d-987f-4d6f3635a448
source_org: Centre for Social Justice (CSJ)
---
```js
"use strict";
 ({params, imports}) => {
  const individual = params.entity;
  const decisions = params.decisions;
  const moment = imports.moment;
  const registrationDecisions = [];
  
  let createdByName = params.user.name;
  let createdByUUID = params.user.uuid;
  let createdByUsername = params.user.username;
  
  const registrationDateForEditCase = individual.getObservationValue("7b1a1540-bfd5-484d-987f-4d6f3635a448");
  if (!registrationDateForEditCase) {
  let regDateTime = moment().format("YYYY-MM-DD HH:mm:ss");
  if (regDateTime) {
  registrationDecisions.push({ name: "Registration DateTime", value: regDateTime });
  }
  }
  
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
