## 标题
Systems Security Engineering: Considerations for a Multidisciplinary Approach in the Engineering of Trustworthy Secure Systems

**标准编号：** NIST SP 800-160, Volume 1

## 正文

<table>
  <tr>
    <th>Withdrawn NIST Technical Series Publication</th>
  </tr>
</table>

<table>
  <tr>
    <th colspan="2">Warning Notice<br><br>The attached publication has been withdrawn (archived), and is provided solely for historical purposes. It may have been superseded by another publication (indicated below).</th>
  </tr>
  <tr>
    <td colspan="2">Withdrawn Publication</td>
  </tr>
  <tr>
    <td>Series/Number</td>
    <td>NIST SP 800-160 Volume 1</td>
  </tr>
  <tr>
    <td>Title</td>
    <td>Systems Security Engineering: Considerations for a Multidisciplinary Approach in the Engineering of Trustworthy Secure Systems</td>
  </tr>
  <tr>
    <td>Publication Date(s)</td>
    <td>March 21, 2018</td>
  </tr>
  <tr>
    <td>Withdrawal Date</td>
    <td>November 16, 2022</td>
  </tr>
  <tr>
    <td>Withdrawal Note</td>
    <td>NIST SP 800-160 Vol. 1 is withdrawn and superseded in its entirety by NIST SP 800-160v1r1</td>
  </tr>
  <tr>
    <td colspan="2">Superseding Publication(s) (if applicable)</td>
  </tr>
  <tr>
    <td colspan="2">The attached publication has been superseded by the following publication(s):</td>
  </tr>
  <tr>
    <td>Series/Number</td>
    <td>NIST SP 800-160v1r1</td>
  </tr>
  <tr>
    <td>Title</td>
    <td>Engineering Trustworthy Secure Systems</td>
  </tr>
  <tr>
    <td>Author(s)</td>
    <td>National Institute of Standards and Technology</td>
  </tr>
  <tr>
    <td>Publication Date(s)</td>
    <td>November 16, 2022</td>
  </tr>
  <tr>
    <td>URL/DOI</td>
    <td>https://doi.org/10.6028/NIST.SP.800-160v1r1</td>
  </tr>
  <tr>
    <td colspan="2">Additional Information (if applicable)</td>
  </tr>
  <tr>
    <td>Contact</td>
    <td>Computer Security Division (Information Technology Laboratory)</td>
  </tr>
  <tr>
    <td>Latest revision of the attached publication</td>
    <td></td>
  </tr>
  <tr>
    <td>Related Information</td>
    <td>https://csrc.nist.gov/projects/systems-security-engineering-project</td>
  </tr>
  <tr>
    <td>Withdrawal Announcement Link</td>
    <td></td>
  </tr>
</table>

Date updated: November 16, 2022

##### Systems Security Engineering

Considerations for a Multidisciplinary Approach in the Engineering of Trustworthy Secure Systems

<table>
  <tr>
    <th>This publication contains systems security engineering considerations for ISO/IEC/IEEE 15288:2015, Systems and software engineering — System life cycle processes. It provides security-related implementation guidance for the standard and should be used in conjunction with and as a complement to the standard.</th>
  </tr>
</table>

RON ROSS

MICHAEL McEVILLEY

JANET CARRIER OREN

This publication is available free of charge from: https://doi.org/10.6028/NIST.SP.800-160v1

##### Systems Security Engineering

Considerations for a Multidisciplinary Approach in the Engineering of Trustworthy Secure Systems

###### RON ROSS

Computer Security Division

National Institute of Standards and Technology

###### MICHAEL McEVILLEY

The MITRE Corporation

###### JANET CARRIER OREN

Legg Mason

This publication is available free of charge from: https://doi.org/10.6028/NIST.SP.800-160v1

###### November 2016

INCLUDES UPDATES AS OF 03-21-2018: PAGE XIII

U.S. Department of Commerce

Penny Pritzker, Secretary

National Institute of Standards and Technology

Willie May, Under Secretary of Commerce for Standards and Technology and Director

###### Authority

This publication has been developed by NIST to further its statutory responsibilities under the Federal Information Security Modernization Act (FISMA) of 2014, 44 U.S.C. § 3551 et seq., Public Law (P.L.) 113-283. NIST is responsible for developing information security standards and guidelines, including minimum requirements for federal information systems, but such standards and guidelines shall not apply to national security systems without the express approval of appropriate federal officials exercising policy authority over such systems. This guideline is consistent with the requirements of the Office of Management and Budget (OMB) Circular A130.

Nothing in this publication should be taken to contradict the standards and guidelines made mandatory and binding on federal agencies by the Secretary of Commerce under statutory authority. Nor should these guidelines be interpreted as altering or superseding the existing authorities of the Secretary of Commerce, Director of the OMB, or any other federal official. This publication may be used by nongovernmental organizations on a voluntary basis and is not subject to copyright in the United States. Attribution would, however, be appreciated by NIST.

National Institute of Standards and Technology Special Publication 800-160

Natl. Inst. Stand. Technol. Spec. Publ. 800-160, Vol. 1, 260 pages (November 2016)

CODEN: NSPUE2

This publication is available free of charge from: https://doi.org/10.6028/NIST.SP.800-160v1

Certain commercial entities, equipment, or materials may be identified in this document in order to describe an experimental procedure or concept adequately. Such identification is not intended to imply recommendation or endorsement by NIST, nor is it intended to imply that the entities, materials, or equipment are necessarily the best available for the purpose.

There may be references in this publication to other publications currently under development by NIST in accordance with its assigned statutory responsibilities. The information in this publication, including concepts, practices, and methodologies, may be used by federal agencies even before the completion of such companion publications. Thus, until each publication is completed, current requirements, guidelines, and procedures, where they exist, remain operative. For planning and transition purposes, federal agencies may wish to closely follow the development of these new publications by NIST.

Organizations are encouraged to review draft publications during the designated public comment periods and provide feedback to NIST. Many NIST cybersecurity publications, other than the ones noted above, are available at http://csrc.nist.gov/publications.

Comments on this publication may be submitted to: National Institute of Standards and Technology Attn: Computer Security Division, Information Technology Laboratory 100 Bureau Drive (Mail Stop 8930) Gaithersburg, MD 20899-8930 Electronic Mail: sec-cert@nist.gov

All comments are subject to release under the Freedom of Information Act.

###### Reports on Computer Systems Technology

The Information Technology Laboratory (ITL) at the National Institute of Standards and Technology (NIST) promotes the U.S. economy and public welfare by providing technical leadership for the Nation’s measurement and standards infrastructure. ITL develops tests, test methods, reference data, proof of concept implementations, and technical analyses to advance the development and productive use of information technology (IT). ITL’s responsibilities include the development of management, administrative, technical, and physical standards and guidelines for the cost-effective security and privacy of other than national security-related information in federal information systems. The Special Publication 800-series reports on ITL’s research, guidelines, and outreach efforts in information systems security and its collaborative activities with industry, government, and academic organizations.

###### Abstract

With the continuing frequency, intensity, and adverse consequences of cyber-attacks, disruptions, hazards, and other threats to federal, state, and local governments, the military, businesses, and the critical infrastructure, the need for trustworthy secure systems has never been more important to the long-term economic and national security interests of the United States. Engineering-based solutions are essential to managing the growing complexity, dynamicity, and interconnectedness of today’s systems, as exemplified by cyber-physical systems and systems-of-systems, including the Internet of Things. This publication addresses the engineering-driven perspective and actions necessary to develop more defensible and survivable systems, inclusive of the machine, physical, and human components that compose the systems and the capabilities and services delivered by those systems. It starts with and builds upon a set of well-established International Standards for systems and software engineering published by the International Organization for Standardization (ISO), the International Electrotechnical Commission (IEC), and the Institute of Electrical and Electronics Engineers (IEEE) and infuses systems security engineering methods, practices, and techniques into those systems and software engineering activities. The objective is to address security issues from a stakeholder protection needs, concerns, and requirements perspective and to use established engineering processes to ensure that such needs, concerns, and requirements are addressed with appropriate fidelity and rigor, early and in a sustainable manner throughout the life cycle of the system.

###### Keywords

Assurance; developmental engineering; disposal; engineering trades; field engineering; implementation; information security; information security policy; inspection; integration; penetration testing; protection needs; requirements analysis; resiliency; review; risk assessment; risk management; risk treatment; security architecture; security authorization; security design; security requirements; specifications; stakeholder; system-of-systems; system component; system element; system life cycle; systems; systems engineering; systems security engineering; trustworthiness; validation; verification.

###### Acknowledgements.

 The authors gratefully acknowledge and appreciate the significant contributions from individuals and organizations in the public and private sectors, whose thoughtful and constructive comments improved the overall quality, thoroughness, and usefulness of this publication. In particular, we wish to thank Beth Abramowitz, Max Allway, Kristen Baldwin, Dawn Beyer, Deb Bodeau, Paul Clark, Keesha Crosby, Judith Dahmann, Kelley Dempsey, Holly Dunlap, Jennifer Fabius, Daniel Faigin, Jeanne Firey, Jim Foti, Robin Gandhi, Rich Graubart, Kevin Greene, Richard Hale, Daryl Hild, Kesha Hill, Peggy Himes, Danny Holtzman, Cynthia Irvine, Brett Johnson, Ken Kepchar, Stephen Khou, Elizabeth Lennon, Alvi Lim, Logan Mailloux, Dennis Mangsen, Doug Maughn, Rosalie McQuaid, Joseph Merkling, John Miller, Thuy Nguyen, Lisa Nordman, Dorian Pappas, Paul Popick, Roger Schell, Thom Schoeffling, Matt Scholl, Peter Sell, Gary Stoneburner, Glenda Turner, Mark Winstead, and William Young for their individual contributions to this publication.

We would also like to extend our sincere appreciation to the National Security Agency; Naval Postgraduate School; Department of Defense Office of Acquisition, Technology, and Logistics; United States Air Force; Department of Homeland Security Science and Technology Office, Cyber Security Division; Air Force Institute of Technology; International Council on Systems Engineering, and The MITRE Corporation, for their ongoing support for the systems security engineering project.

Finally, the authors respectfully acknowledge the seminal work in computer security that dates back to the 1960s. The vision, insights, and dedicated efforts of those early pioneers in computer security serve as the philosophical and technical foundation for the security principles, concepts, and practices employed in this publication to address the critically important problem of engineering trustworthy secure systems.

###### Prologue

“Among the forces that threaten the United States and its interests are those that blend the lethality and high-tech capabilities of modern weaponry with the power and opportunity of asymmetric tactics such as terrorism and cyber warfare. We are challenged not only by novel employment of conventional weaponry, but also by the hybrid nature of these threats. We have seen their effects on the American homeland. Moreover, we must remember that we face a determined and constantly adapting adversary.”

Quadrennial Homeland Security Review Report

February 2010

###### Foreword

The United States has developed incredibly powerful and complex systems—systems that are inexorably linked to the economic and national security interests of the Nation. The complete dependence on those systems for mission and business success in both the public and private sectors, including the critical infrastructure, has left the Nation extremely vulnerable to hostile cyber-attacks and other serious threats, including natural disasters, structural/component failures, and errors of omission and commission. The susceptibility to such threats was described in the January 2013 Defense Science Board Task Force Report entitled Resilient Military Systems and the Advanced Cyber Threat. The reported concluded that—

“…the cyber threat is serious and that the United States cannot be confident that our critical Information Technology systems will work under attack from a sophisticated and well-resourced opponent utilizing cyber capabilities in combination with all of their military and intelligence capabilities (a full spectrum adversary) …”

The Task Force stated that the susceptibility to the advanced cyber threat by the Department of Defense is also a concern for public and private networks, in general, and recommended that steps be taken immediately to build an effective response to measurably increase confidence in the systems we depend on (in the public and private sectors) and at the same time, decrease a would-be attacker's confidence in the effectiveness of their capabilities to compromise those systems. This conclusion was based on the following facts:

- • The success adversaries have had in penetrating our networks;
- • The relative ease that our Red Teams have in disrupting, or completely defeating, our forces in exercises using exploits available on the Internet; and
- • The weak security posture of our networks and systems.

The Task Force also described several tiers of vulnerabilities within organizations including known vulnerabilities, unknown vulnerabilities, and adversary-created vulnerabilities. The important and sobering message conveyed by the Defense Science Board is that the top two tiers of vulnerabilities (i.e., the unknown vulnerabilities and adversary-created vulnerabilities) are, for the most part, totally invisible to most organizations. These vulnerabilities can be effectively addressed by sound systems security engineering techniques, methodologies, processes, and practices—in essence, providing the necessary trustworthiness to withstand and survive well-resourced, sophisticated cyber-attacks on the systems supporting critical missions and business operations.

To begin to address the challenges of the 21st century, we must:

- • Understand the modern threat space (i.e., adversary capabilities and intentions revealed by the targeting actions of those adversaries);
- • Identify stakeholder assets and protection needs and provide protection commensurate with the criticality of those assets and needs and the consequences of asset loss;
- • Increase the understanding of the growing complexity of systems—to more effectively reason about, manage, and address the uncertainty associated with that complexity;
- • Integrate security requirements, functions, and services into the mainstream management and technical processes within the life cycle processes of systems; and
- • Build trustworthy secure systems capable of protecting stakeholder assets.

###### SYSTEM SECURITY AS A DESIGN PROBLEM

“Providing satisfactory security controls in a computer system is in itself a system design problem. A combination of hardware, software, communications, physical, personnel and administrative-procedural safeguards is required for comprehensive security. In particular, software safeguards alone are not sufficient.”

-- The Ware Report Defense Science Board Task Force on Computer Security, 1970.

This publication addresses the engineering-driven actions necessary to develop more defensible and survivable systems—including the components that compose and the services that depend on those systems. It starts with and builds upon a set of well-established International Standards for systems and software engineering published by the International Organization for Standardization (ISO), the International Electrotechnical Commission (IEC), and the Institute of Electrical and Electronics Engineers (IEEE), and infuses systems security engineering techniques, methods, and practices into those systems and software engineering activities. The ultimate objective is to address security issues from a stakeholder requirements and protection needs perspective and to use established engineering processes to ensure that such requirements and needs are addressed with the appropriate fidelity and rigor across the entire life cycle of the system.

Increasing the trustworthiness of systems is a significant undertaking that requires a substantial investment in the requirements, architecture, design, and development of systems, components, applications, and networks—and a fundamental cultural change to the current “business as usual” approach. Introducing a disciplined, structured, and standards-based set of systems security engineering activities and tasks provides an important starting point and forcing function to initiate needed change. The ultimate objective is to obtain trustworthy secure systems that are fully capable of supporting critical missions and business operations while protecting stakeholder assets, and to do so with a level of assurance that is consistent with the risk tolerance of those stakeholders.

-- Ron Ross

National Institute of Standards and Technology

###### DISCLAIMER

This publication is intended to be used in conjunction with and as a supplement to International Standard ISO/IEC/IEEE 15288, Systems and software engineering — System life cycle processes. It is strongly recommended that organizations using this publication obtain the standard in order to fully understand the context of the security-related activities and tasks in each of the system life cycle processes. Content from the international standard that is referenced in this publication is reprinted with permission from the Institute of Electrical and Electronics Engineers and is noted as follows:

ISO/IEC/IEEE 15288-2015. Reprinted with permission from IEEE, Copyright IEEE 2015, All rights reserved.

###### HOW TO USE THIS PUBLICATION

This publication is intended to be flexible in its application in order to meet the diverse needs of organizations. It is not intended to provide a specific recipe for execution. Rather, it can be viewed as a catalog or handbook for achieving the identified security outcomes of a systems engineering perspective on system life cycle processes—leaving it to the experience and expertise of the engineering organization to determine what is correct for its purpose. Thus, organizations choosing to use this guidance for their systems security engineering efforts can select and employ some or all of the thirty ISO/IEC/IEEE 15288 processes and some or all of the security-related activities and tasks defined for each process. Note that there are process dependencies, and the successful completion of some activities and tasks necessarily invokes other processes or leverages the results of other processes.

The system life cycle processes can be used for new systems, system upgrades, or systems that are being repurposed; can be employed at any stage of the system life cycle; and can take advantage of any system or software development methodology including, for example, waterfall, spiral, or agile. The processes can also be applied recursively, iteratively, concurrently, sequentially, or in parallel and to any system regardless of its size, complexity, purpose, scope, environment of operation, or special nature.

The full extent of the application of the content in this publication is informed by stakeholder capability, protection needs, and concerns with particular attention to considerations of cost, schedule, and performance. The tailorable nature of the engineering activities and tasks and the system life cycle processes will ensure that the specific systems resulting from the application of the security design principles and concepts have the level of trustworthiness deemed sufficient to protect stakeholders from suffering unacceptable loss of assets and the associated consequences. Such trustworthiness is made possible by the rigorous application of those design principles and concepts within a disciplined and structured set of processes that provides the necessary evidence and transparency to support risk-informed decision making and trades.

CONTEXT-SENSITIVE SECURITY

Getting the Maximum Benefit from This Publication

This publication is not intended to formally define Systems Security Engineering (SSE); make a definitive or authoritative statement of what SSE is and what it is not; define or prescribe a specific process; or prescribe a mandatory set of activities for compliance purposes. This publication is intended to address the activities and tasks, the concepts and principles, and most importantly, what needs to be “considered” from a security perspective when executing within the context of Systems Engineering (hence the alignment to the international standard ISO/IEC/IEEE 15288). The title of the publication, Systems Security Engineering — Considerations for a Multidisciplinary Approach in the Engineering of Trustworthy Secure Systems, was chosen to appropriately convey how the content can be used to achieve the maximum benefit.

- • The use of the term “considerations” is intended to emphasize that this document is not claiming to be “the” answer for the formal statement of SSE and all forms of its application. It does not define SSE, but rather offers considerations towards what can and should be done now and from which there can be continued evolution and maturation towards more effective and context-sensitive application of the considerations to address the breadth and depth of system security problems. In that regard, the document is not “a process” but a collection of related processes, where each process addresses an aspect of the system security problem space and offers a cohesive set of activities, tasks, and outcomes that combine to achieve the end goal of a trustworthy secure system. The application of any process must be properly calibrated to the objectives and constraints in the context to which the process is applied and conducted with an appropriate level of rigor.
- • The use of the term “in the engineering of” is intended to emphasize that the focus is on engineering (as opposed to building, integrating, or assembling). The core objective of the publication is to be engineering-based, not operations- or technology-based. Considerations are grounded in a systems engineering viewpoint of system life cycle processes. Organizations using the publication will certainly tailor the life cycle processes for effectiveness, feasibility, and practicality, but in doing so they have the responsibility to achieve the stated outcomes nonetheless. There can be legitimate variances with the activities and tasks and how they are or are not accomplished, or whether they do or do not have value in the particular context of their application. These variances occur when differing and sometimes conflicting views must be addressed and traded among to achieve the combined objectives of all stakeholders in a cost-effective manner.

Note: Context-sensitive security means that stakeholders establish the value of their assets and the context to subsequently apply the SSE activities and tasks that provide a level of asset protection and trustworthiness that falls within their tolerance of loss and associated risk—through custom development and fabrication to the procurement of commercial products and services to achieve the required level of protection and trustworthiness. Context-sensitive application of the SSE activities and tasks in this publication is precisely what systems engineering expects. With sufficient understanding of SSE, the context-sensitive application happens as a natural by-product of systems engineering. It is essential that the processes be adaptable and tailorable to address the complexity and dynamicity of all factors that define the system and its environmental context. This includes the system-of-systems environment where such systems may not have a single owner, may not be under a single authority, or may not operate within a single set of priorities. The system-of-systems context potentially requires the execution of these processes along a different line of reasoning. The fundamentals and concepts of SSE are still applicable, but may have to be applied differently. This is one of the primary design objectives for the Systems Security Engineering Framework and the associated SSE activities and tasks provided in this publication.

###### NIST SYSTEMS SECURITY ENGINEERING INITIATIVE

NIST Special Publication 800-160 is the flagship publication in a series of planned systems security engineering publications. The series of 800-160 publications will include several important systems security engineering topics, for example: hardware security and assurance; software security and assurance; and system resiliency. Each topic will be addressed in the context of the system life cycle processes contained in ISO/IEC/IEEE 15288 and the security-related activities and tasks that are described in SP 800-160.

NIST plans to update its foundational security and risk management guidance to describe how such guidance might be interpreted and applied at both the enterprise level and in association with systems engineering processes.

###### Errata

This table contains changes that have been incorporated into Special Publication 800-160, Volume 1. Errata updates can include corrections, clarifications, or other minor changes in the publication that are either editorial or substantive in nature.

<table>
  <tr>
    <th>DATE</th>
    <th>TYPE</th>
    <th>REVISION</th>
    <th>PAGE</th>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Editorial</td>
    <td>HOW TO USE THIS PUBLICATION call out box, Line 1: Delete “extremely”</td>
    <td>x</td>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Editorial</td>
    <td>Chapter 1, Introduction, Paragraph 1, Line 9: Delete “in order”</td>
    <td>1</td>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Editorial</td>
    <td>Chapter 1, Introduction, Paragraph 3, Line 1: Delete “basic”</td>
    <td>1</td>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Editorial</td>
    <td>Chapter 1, Introduction, Paragraph 3, Line 1: Change “disciplined” to “disciplined and structured”</td>
    <td>1</td>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Editorial</td>
    <td>Chapter 1, Introduction, Paragraph 8, Line 2: Change “taking action” to “acting”</td>
    <td>3</td>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Substantive</td>
    <td>Chapter 1, Introduction: Add call out box “ESTABLISHING THE TRUSTWORTHINESS OF SYSTEMS AND COMPONENTS”</td>
    <td>3</td>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Editorial</td>
    <td>Chapter 1, Section 1.2, Paragraph 1, Line 4: Delete “or all”</td>
    <td>6</td>
  </tr>
  <tr>
    <td>01-03-2018</td>
    <td>Editorial</td>
    <td>Chapter 2,